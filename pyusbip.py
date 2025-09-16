#!/usr/bin/env python3

# pyusbip
# USBIP server in Python
#
# Copyright (c) 2018 Joshua Wise
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import dataclasses
import logging
import struct
import sys
import os

import usb1


USBIP_HOST = "127.0.0.1"
USBIP_PORT = 3240

USBIP_REQUEST = 0x8000
USBIP_REPLY = 0x0000

USBIP_OP_UNSPEC = 0x00
USBIP_OP_DEVINFO = 0x02
USBIP_OP_IMPORT = 0x03
USBIP_OP_EXPORT = 0x06
USBIP_OP_UNEXPORT = 0x07
USBIP_OP_DEVLIST = 0x05

USBIP_CMD_SUBMIT = 0x0001
USBIP_CMD_UNLINK = 0x0002
USBIP_RET_SUBMIT = 0x0003
USBIP_RET_UNLINK = 0x0004
USBIP_RESET_DEV = 0xFFFF

USBIP_DIR_OUT = 0
USBIP_DIR_IN = 1

USBIP_ST_OK = 0x00
USBIP_ST_NA = 0x01

USBIP_BUS_ID_SIZE = 32
USBIP_DEV_PATH_MAX = 256

USBIP_VERSION = 0x0111

USBIP_SPEED_UNKNOWN = 0
USBIP_SPEED_LOW = 1
USBIP_SPEED_FULL = 2
USBIP_SPEED_HIGH = 3
USBIP_SPEED_VARIABLE = 4

USB_RECIP_DEVICE = 0x00
USB_RECIP_INTERFACE = 0x01
USB_REQ_SET_ADDRESS = 0x05
USB_REQ_SET_CONFIGURATION = 0x09
USB_REQ_SET_INTERFACE = 0x0B

USB_ENDPOINT_XFERTYPE_MASK = 0x03
USB_ENDPOINT_XFER_CONTROL = 0
USB_ENDPOINT_XFER_ISOC = 1
USB_ENDPOINT_XFER_BULK = 2
USB_ENDPOINT_XFER_INT = 3

USB_ENOENT = 2
USB_EPIPE = 32

# Configure logging early
logging.basicConfig(
    level=logging.DEBUG if "-v" in sys.argv else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pyusbip")

usbctx = usb1.USBContext()
usbctx.open()


class USBIPUnimplementedException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class USBIPProtocolErrorException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


@dataclasses.dataclass
class USBIPDevice:
    devid: int
    hnd: usb1.USBDeviceHandle
    endpoint_types: dict[int, int] = dataclasses.field(default_factory=dict)
    alt_settings: dict[int, int] = dataclasses.field(default_factory=dict)
    detached_kernel: bool = False


@dataclasses.dataclass
class USBIPPending:
    seqnum: int
    device: USBIPDevice
    xfer: usb1.USBTransfer


class USBIPConnection:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.devices: dict[int, USBIPDevice] = {}
        self.urbs: dict[int, USBIPPending] = {}

        peer = writer.get_extra_info("peername")
        if isinstance(peer, tuple) and len(peer) >= 2:
            peer = f"{peer[0]}:{peer[1]}"
        self.peer = str(peer)
        self.logger = logging.getLogger(f"pyusbip.connection[{self.peer}]")

    def rebuild_endpoint_map(self, dev: USBIPDevice) -> None:
        """Rebuild endpoint->transfer-type map for the active configuration.

        Uses dev.alt_settings (defaulting to 0 per interface) to select the
        active alternate setting when enumerating endpoints.
        """
        try:
            cfg_val = dev.hnd.getConfiguration()
        except usb1.USBError as e:
            # Fallback to first configuration if querying fails
            self.logger.debug("getConfiguration failed: %s", e)
            try:
                cfg_val = next(
                    dev.hnd.getDevice().iterConfigurations()
                ).getConfigurationValue()
            except StopIteration:
                self.logger.debug(
                    "no configurations available while rebuilding map"
                )
                return

        cfg = None
        for _cfg in dev.hnd.getDevice().iterConfigurations():
            if _cfg.getConfigurationValue() == cfg_val:
                cfg = _cfg
                break
        if cfg is None:
            return

        new_map: dict[int, int] = {}
        type_name = {
            USB_ENDPOINT_XFER_CONTROL: "CTRL",
            USB_ENDPOINT_XFER_ISOC: "ISOC",
            USB_ENDPOINT_XFER_BULK: "BULK",
            USB_ENDPOINT_XFER_INT: "INT",
        }
        for i, ifc in enumerate(cfg.iterInterfaces()):
            # Choose desired alt setting if available, else first
            target_alt = dev.alt_settings.get(i, 0)
            chosen = None
            for setting in ifc:
                if setting.getAlternateSetting() == target_alt:
                    chosen = setting
                    break
            if chosen is None:
                chosen = list(ifc)[0]
                dev.alt_settings[i] = chosen.getAlternateSetting()
            else:
                dev.alt_settings.setdefault(i, target_alt)

            ep_summaries = []
            for epd in chosen.iterEndpoints():
                addr = epd.getAddress()
                xfertype = epd.getAttributes() & USB_ENDPOINT_XFERTYPE_MASK
                new_map[addr] = xfertype
                ep_summaries.append(
                    f"0x{addr:02x} {type_name.get(xfertype, str(xfertype))}"
                )
            self.logger.debug(
                "iface %d alt %d: %s",
                i,
                dev.alt_settings.get(i, 0),
                ", ".join(ep_summaries),
            )

        dev.endpoint_types = new_map

    @staticmethod
    def ep_addr(ep: int, direction: int) -> int:
        return (ep & 0x0F) | ((1 if direction else 0) << 7)

    @staticmethod
    def pack_ret_submit(
        seqnum: int, status: int, actual_len: int, data: bytes = b""
    ) -> bytes:
        hdr = struct.pack(
            ">IIIIIiiiii8s",
            USBIP_RET_SUBMIT,
            seqnum,
            0,
            0,
            0,
            status,
            actual_len,
            0,
            0,
            0,
            b"",
        )
        return hdr + (data or b"")

    @staticmethod
    def pack_ret_unlink(seqnum: int, rv: int) -> bytes:
        return struct.pack(
            ">IIIIIiiiii8s",
            USBIP_RET_UNLINK,
            seqnum,
            0,
            0,
            0,
            rv,
            0,
            0,
            0,
            0,
            b"",
        )

    def claim_and_rebuild(self, dev: USBIPDevice, cfg_value: int) -> None:
        # Claim all interfaces for the specified configuration and rebuild maps
        config = None
        for _config in dev.hnd.getDevice().iterConfigurations():
            if _config.getConfigurationValue() == cfg_value:
                config = _config
                break
        if config is None:
            return
        for i in range(config.getNumInterfaces()):
            self.logger.debug("  claim interface: %d", i)
            dev.hnd.claimInterface(i)
        dev.alt_settings = {}
        self.rebuild_endpoint_map(dev)

    def handle_control_transfer(
        self,
        dev: USBIPDevice,
        setup: bytes,
        direction: int,
        out_buf: bytes,
    ) -> tuple[int, int, bytes]:
        # Returns (status, actual_len, data)
        (bRequestType, bRequest, wValue, wIndex, wLength) = struct.unpack(
            "<BBHHH", setup
        )

        self.logger.debug(
            "EP0 requesttype %d, request %d", bRequestType, bRequest
        )

        fakeit = False

        if bRequestType == USB_RECIP_DEVICE and bRequest == USB_REQ_SET_ADDRESS:
            raise USBIPUnimplementedException("USB_REQ_SET_ADDRESS")
        elif (
            bRequestType == USB_RECIP_DEVICE
            and bRequest == USB_REQ_SET_CONFIGURATION
        ):
            self.logger.info("set configuration: %d", wValue)
            dev.hnd.setConfiguration(wValue)
            self.claim_and_rebuild(dev, wValue)
            fakeit = True
        elif (
            bRequestType == USB_RECIP_INTERFACE
            and bRequest == USB_REQ_SET_INTERFACE
        ):
            self.logger.info(
                "set interface alt setting: %d -> %d", wIndex, wValue
            )
            dev.hnd.claimInterface(wIndex)
            dev.hnd.setInterfaceAltSetting(wIndex, wValue)
            dev.alt_settings[wIndex] = wValue
            self.rebuild_endpoint_map(dev)
            fakeit = True

        try:
            if direction == USBIP_DIR_IN:
                data = dev.hnd.controlRead(
                    bRequestType, bRequest, wValue, wIndex, wLength
                )
                return (0, len(data), data)
            else:
                if fakeit:
                    wlen = 0
                else:
                    wlen = dev.hnd.controlWrite(
                        bRequestType, bRequest, wValue, wIndex, out_buf
                    )
                return (0, wlen, b"")
        except usb1.USBErrorPipe:
            self.logger.warning("EPIPE during control transfer")
            return (-USB_EPIPE, 0, b"")

    def claim_device(
        self, dev: usb1.USBDevice, hnd: usb1.USBDeviceHandle
    ) -> USBIPDevice:
        """Detach kernel drivers if needed and prepare the USBIPDevice.

        - Checks each interface in the active configuration for a kernel driver
          and detaches if running as root.
        - Builds the endpoint map and initializes alt settings.
        - Returns a populated USBIPDevice instance.
        """
        # Determine active configuration
        try:
            cfg_val = hnd.getConfiguration()
        except usb1.USBError as e:
            self.logger.debug("getConfiguration failed during claim: %s", e)
            try:
                cfg_val = next(dev.iterConfigurations()).getConfigurationValue()
            except StopIteration:
                self.logger.debug("no configurations available during claim")
                cfg_val = 0

        detached_any = False
        for cfg in dev.iterConfigurations():
            if cfg.getConfigurationValue() != cfg_val:
                continue
            for ifc in cfg.iterInterfaces():
                # Use first setting to get interface number
                try:
                    ifn = list(ifc)[0].getNumber()
                except (IndexError, TypeError, AttributeError) as e:
                    self.logger.debug("failed to get interface number: %s", e)
                    continue
                try:
                    active = hnd.kernelDriverActive(ifn)
                except usb1.USBError as e:
                    self.logger.debug(
                        "kernelDriverActive failed on if %d: %s", ifn, e
                    )
                    active = False
                if active:
                    self.logger.warning(
                        "kernel driver active on interface %d", ifn
                    )
                    # On macOS, detaching typically requires root; on Linux, attempt regardless
                    is_macos = sys.platform.startswith("darwin")
                    if is_macos and os.geteuid() != 0:
                        self.logger.info(
                            "macOS non-root; not detaching kernel driver on interface %d",
                            ifn,
                        )
                    else:
                        try:
                            hnd.detachKernelDriver(ifn)
                            detached_any = True
                            self.logger.info(
                                "detached kernel driver on interface %d", ifn
                            )
                        except usb1.USBError as e:
                            self.logger.warning(
                                "failed to detach kernel driver on interface %d",
                                ifn,
                            )
                            self.logger.debug("detachKernelDriver error: %s", e)

        devid = dev.getBusNumber() << 16 | dev.getDeviceAddress()
        usbip_dev = USBIPDevice(
            devid=devid, hnd=hnd, detached_kernel=detached_any
        )
        # Build initial endpoint map
        self.rebuild_endpoint_map(usbip_dev)
        return usbip_dev

    def pack_device_desc(
        self, dev: usb1.USBDevice, interfaces: bool = True
    ) -> bytes:
        """Takes a usb1 device and packs it into a struct usb_device (and
        optionally, struct usb_interfaces)."""

        path = f"pyusbip/{dev.getBusNumber()}/{dev.getDeviceAddress()}"
        busid = f"{dev.getBusNumber()}-{dev.getDeviceAddress()}"
        busnum = dev.getBusNumber()
        devnum = dev.getDeviceAddress()
        speed = {
            usb1.SPEED_UNKNOWN: USBIP_SPEED_UNKNOWN,
            usb1.SPEED_LOW: USBIP_SPEED_LOW,
            usb1.SPEED_FULL: USBIP_SPEED_FULL,
            usb1.SPEED_HIGH: USBIP_SPEED_HIGH,
            usb1.SPEED_SUPER: USBIP_SPEED_HIGH,
        }[dev.getDeviceSpeed()]

        idVendor = dev.getVendorID()
        idProduct = dev.getProductID()
        bcdDevice = dev.getbcdDevice()

        bDeviceClass = dev.getDeviceClass()
        bDeviceSubClass = dev.getDeviceSubClass()
        bDeviceProtocol = dev.getDeviceProtocol()
        configs = list(dev.iterConfigurations())
        try:
            hnd = dev.open()
            bConfigurationValue = hnd.getConfiguration()
            hnd.close()
        except usb1.USBError as e:
            self.logger.debug(
                "pack_device_desc: getConfiguration via open failed: %s", e
            )
            bConfigurationValue = configs[0].getConfigurationValue()
        bNumConfigurations = dev.getNumConfigurations()

        # Sigh, find it.
        config = configs[0]
        for _config in configs:
            if _config.getConfigurationValue() == bConfigurationValue:
                config = _config
                break
        bNumInterfaces = config.getNumInterfaces()

        data = struct.pack(
            ">256s32sIIIHHHBBBBBB",
            path.encode(),
            busid.encode(),
            busnum,
            devnum,
            speed,
            idVendor,
            idProduct,
            bcdDevice,
            bDeviceClass,
            bDeviceSubClass,
            bDeviceProtocol,
            bConfigurationValue,
            bNumConfigurations,
            bNumInterfaces,
        )

        if interfaces:
            for ifc in config.iterInterfaces():
                set = list(ifc)[0]
                data += struct.pack(
                    ">BBBB",
                    set.getClass(),
                    set.getSubClass(),
                    set.getProtocol(),
                    0,
                )

        return data

    def handle_op_devlist(self) -> None:
        devlist = usbctx.getDeviceList()

        resp = struct.pack(
            ">HHII",
            USBIP_VERSION,
            USBIP_OP_DEVLIST | USBIP_REPLY,
            USBIP_ST_OK,
            len(devlist),
        )
        for dev in devlist:
            resp += self.pack_device_desc(dev)

        self.writer.write(resp)

    def handle_op_import(self, busid: str) -> None:
        # We kind of do this the hard way -- rather than looking up by bus
        # id / device address, we instead just compare the string.  Life is
        # too short to extend python-libusb1.
        devlist = usbctx.getDeviceList()
        for dev in devlist:
            dev_busid = f"{dev.getBusNumber()}-{dev.getDeviceAddress()}"
            if busid == dev_busid:
                hnd = dev.open()
                self.logger.info("opened device %s", busid)
                claimed = self.claim_device(dev, hnd)
                self.devices[claimed.devid] = claimed
                resp = struct.pack(
                    ">HHI",
                    USBIP_VERSION,
                    USBIP_OP_IMPORT | USBIP_REPLY,
                    USBIP_ST_OK,
                )
                resp += self.pack_device_desc(dev, interfaces=False)
                self.writer.write(resp)
                return

        self.logger.warning("device not found")
        resp = struct.pack(
            ">HHI", USBIP_VERSION, USBIP_OP_IMPORT | USBIP_REPLY, USBIP_ST_NA
        )
        self.writer.write(resp)

    async def handle_urb_submit(
        self, seqnum: int, dev: USBIPDevice, direction: int, ep: int
    ) -> None:
        op_submit = ">Iiiii8s"
        data = await self.reader.readexactly(struct.calcsize(op_submit))
        (
            transfer_flags,
            buflen,
            start_frame,
            number_of_packets,
            interval,
            setup,
        ) = struct.unpack(op_submit, data)

        if number_of_packets != 0:
            raise USBIPUnimplementedException(
                f"ISO number_of_packets {number_of_packets}"
            )

        out_buf = b""
        if direction == USBIP_DIR_OUT:
            out_buf = await self.reader.readexactly(buflen)

        self.logger.debug(
            "seq %x: ep %d, direction %d, %d bytes",
            seqnum,
            ep,
            direction,
            buflen,
        )

        if ep == 0:
            # EP0 control traffic; handle synchronously via helper
            (status, actual_len, data_bytes) = self.handle_control_transfer(
                dev, setup, direction, out_buf
            )
            if (
                direction == USBIP_DIR_IN
                and actual_len != struct.unpack("<BBHHH", setup)[4]
            ):
                self.logger.debug(
                    "wrote response with %d/%d bytes",
                    actual_len,
                    struct.unpack("<BBHHH", setup)[4],
                )
            resp = self.pack_ret_submit(seqnum, status, actual_len, data_bytes)
            self.writer.write(resp)
            return

        # Non-EP0 endpoints: asynchronous path unified
        xfer = dev.hnd.getTransfer()

        def done_callback(xfer_):
            status = xfer.getStatus()
            actual = xfer.getActualLength()
            self.logger.debug(
                "callback seqnum %x status %d len %d",
                seqnum,
                status,
                actual,
            )
            data = b""
            if direction == USBIP_DIR_IN:
                data = xfer.getBuffer()[:actual]
            self.writer.write(
                self.pack_ret_submit(seqnum, -status, actual, data)
            )
            del self.urbs[seqnum]

        # Choose transfer type based on endpoint descriptor
        addr = self.ep_addr(ep, direction)
        xfertype = dev.endpoint_types.get(addr)
        if xfertype == USB_ENDPOINT_XFER_INT:
            xfer.setInterrupt(
                addr,
                buflen if direction == USBIP_DIR_IN else out_buf,
                done_callback,
            )
        else:
            xfer.setBulk(
                addr,
                buflen if direction == USBIP_DIR_IN else out_buf,
                done_callback,
            )
        xfer.submit()
        self.urbs[seqnum] = USBIPPending(seqnum, dev, xfer)

    async def handle_urb_unlink(
        self, seqnum: int, dev: USBIPDevice, direction: int, ep: int
    ) -> None:
        op_submit = ">Iiiii8s"
        data = await self.reader.readexactly(struct.calcsize(op_submit))
        (sseqnum, buflen, start_frame, number_of_packets, interval, setup) = (
            struct.unpack(op_submit, data)
        )

        self.logger.debug("seq %x: UNLINK", sseqnum)

        if sseqnum not in self.urbs:
            rv = -USB_ENOENT
        else:
            rv = 0
            self.urbs[sseqnum].xfer.cancel()

        self.writer.write(self.pack_ret_unlink(seqnum, rv))

    async def handle_packet(self):
        """
        Handle a USBIP packet.
        """

        # Try to read a header of some kind.  We can tell because if it's an
        # URB, the |op_common.version| is overlayed with the
        # |usbip_header_basic.command|, and so the |.version| is 0x0000;
        # otherwise, it's supposed to be 0x0106.

        try:
            data = await self.reader.readexactly(2)
        except asyncio.IncompleteReadError:
            return False

        (version,) = struct.unpack(">H", data)
        if version == 0x0000:
            # Note that we've already trimmed the version.
            op_common = ">HIIII"
            data = await self.reader.readexactly(struct.calcsize(op_common))
            (opcode, seqnum, devid, direction, ep) = struct.unpack(
                op_common, data
            )

            if devid not in self.devices:
                raise USBIPProtocolErrorException(f"devid unattached {devid:x}")
            dev = self.devices[devid]

            if opcode == USBIP_CMD_SUBMIT:
                await self.handle_urb_submit(seqnum, dev, direction, ep)
            elif opcode == USBIP_CMD_UNLINK:
                await self.handle_urb_unlink(seqnum, dev, direction, ep)
            elif opcode == USBIP_RESET_DEV:
                raise USBIPUnimplementedException("URB_RESET_DEV")
            else:
                raise USBIPProtocolErrorException(f"bad USBIP URB {opcode:x}")
        elif (version & 0xFF00) == 0x0100:
            # Note that we've already trimmed the version.
            op_common = ">HI"
            data = await self.reader.readexactly(struct.calcsize(op_common))
            (opcode, status) = struct.unpack(op_common, data)

            if opcode == USBIP_OP_UNSPEC | USBIP_REQUEST:
                self.writer.write(
                    struct.pack(
                        ">HHI",
                        version,
                        USBIP_OP_UNSPEC | USBIP_REPLY,
                        USBIP_ST_OK,
                    )
                )
            elif opcode == USBIP_OP_DEVINFO | USBIP_REQUEST:
                data = await self.reader.readexactly(USBIP_BUS_ID_SIZE)
                raise USBIPUnimplementedException("DEVINFO")
                # writer.write(struct.pack(">HHI", version, USBIP_OP_DEVINFO | USBIP_REPLY, USBIP_ST_NA)
            elif opcode == USBIP_OP_DEVLIST | USBIP_REQUEST:
                self.logger.debug("DEVLIST")
                # XXX: in theory, op_devlist_request has a _reserved, but they don't seem to xmit it?
                # data = await self.reader.readexactly(4) # reserved
                self.handle_op_devlist()
            elif opcode == USBIP_OP_IMPORT | USBIP_REQUEST:
                data = (
                    (await self.reader.readexactly(USBIP_BUS_ID_SIZE))
                    .decode()
                    .rstrip("\0")
                )
                self.logger.debug("IMPORT %s", data)
                self.handle_op_import(data)
            else:
                raise USBIPProtocolErrorException(f"bad USBIP op {opcode:x}")
        else:
            raise USBIPProtocolErrorException(
                f"unsupported USBIP version {version:02x}"
            )

        return True

    async def connection(self):
        self.logger.info("connect")

        while True:
            try:
                success = await self.handle_packet()
                await self.writer.drain()
                if not success:
                    break
            except Exception:
                self.logger.exception("force disconnect due to exception")
                break

        self.logger.info("disconnect")
        for devid, dev in list(self.devices.items()):
            try:
                if dev and dev.detached_kernel:
                    try:
                        self.logger.info(
                            "resetting device 0x%08x due to prior kernel detach",
                            devid,
                        )
                        dev.hnd.resetDevice()
                    except usb1.USBError as e:
                        self.logger.warning(
                            "device reset failed for 0x%08x", devid
                        )
                        self.logger.debug("resetDevice error: %s", e)
            finally:
                dev.hnd.close()
                del self.devices[devid]
        await self.writer.drain()
        self.writer.close()
        await self.writer.wait_closed()


def setup_libusb_event_sources():
    loop = asyncio.get_running_loop()

    def on_pollfd_added(fd, events):
        logger.debug("Adding libusb pollfd %d", fd)
        loop.add_reader(fd, usbctx.handleEventsTimeout)

    def on_pollfd_removed(fd, events):
        logger.debug("Removing libusb pollfd %d", fd)
        loop.remove_reader(fd)

    # Handle any existing fds
    for fd, events in usbctx.getPollFDList():
        on_pollfd_added(fd, events)

    # Register for future changes
    usbctx.setPollFDNotifiers(on_pollfd_added, on_pollfd_removed)


async def main():
    setup_libusb_event_sources()

    async def on_usbip_connection(reader, writer):
        conn = USBIPConnection(reader, writer)
        await conn.connection()

    server = await asyncio.start_server(
        on_usbip_connection, USBIP_HOST, USBIP_PORT
    )

    addr = server.sockets[0].getsockname()
    logger.info("Serving on %s", addr)

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Shutting down...")
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
