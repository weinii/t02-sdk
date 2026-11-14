"""Minimal Phomemo T02 SDK (BLE, 384 dots wide). Needs: bleak, pillow."""
import asyncio
from bleak import BleakClient, BleakScanner
from PIL import Image, ImageOps

SERVICE = "0000ff00-0000-1000-8000-00805f9b34fb"
WRITE = "0000ff02-0000-1000-8000-00805f9b34fb"   # write without response
NOTIFY = "0000ff03-0000-1000-8000-00805f9b34fb"  # credits + status
WIDTH = 384                                      # dots @203dpi, 48 bytes/row
INIT_QUERIES = (0x7B, 0x20, 0x45, 0x0E, 0x07, 0x5E, 0x08, 0x09, 0x38, 0x8B, 0x63)
STATUS = {  # notification -> meaning
    bytes.fromhex("1a03a9"): "too hot", bytes.fromhex("1a05 99".replace(" ", "")): "cover open",
    bytes.fromhex("1a0688"): "paper missing", bytes.fromhex("1a0f0c"): "print done",
    bytes.fromhex("1a0bb8"): "job cancelled",
}


def to_raster(img: Image.Image, darkness: float = 1.0, autocontrast: bool = False) -> bytes:
    """PIL image -> 384px wide 1-bit rows, MSB first, 1 = black.

    darkness > 1 prints darker, < 1 lighter (software gamma; the T02 density command is unknown).
    """
    img = ImageOps.exif_transpose(img)
    if "A" in img.getbands() or "transparency" in img.info:  # flatten transparency onto white
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, "white"); bg.alpha_composite(img); img = bg
    img = img.convert("L")
    if autocontrast:
        img = ImageOps.autocontrast(img, cutoff=1)
    if darkness != 1.0:
        img = img.point(lambda v: round(255 * (v / 255) ** darkness))
    img = img.resize((WIDTH, round(img.height * WIDTH / img.width)))
    img = img.convert("1")  # Floyd-Steinberg dither; PIL "1": 255 = white
    img = ImageOps.invert(img.convert("L")).convert("1")
    return img.tobytes()  # rows of 48 bytes


def job_bytes(img: Image.Image, feed: int = 4, block: int = 65535, **kw) -> bytes:
    data = to_raster(img, **kw)
    rows = len(data) // 48
    out = bytearray(b"\x1b\x40\x1b\x61\x01\x1f\x11\x35\x00")  # reset, center, uncompressed
    for y in range(0, rows, block):  # rows per raster block; 255-row blocks left visible seams on the T02
        n = min(block, rows - y)
        out += b"\x1d\x76\x30\x00\x30\x00" + n.to_bytes(2, "little") + data[y * 48:(y + n) * 48]
    return bytes(out + b"\x1b\x64" + bytes([feed]))


class T02:
    def __init__(self, address: str | None = None):
        self.address, self.limit, self.credits = address, 20, 1
        self.status, self._ev, self._done = [], asyncio.Event(), asyncio.Event()

    async def __aenter__(self):
        dev = self.address or await BleakScanner.find_device_by_filter(
            lambda d, a: (d.name or "").startswith("T02"), timeout=10)
        if not dev:
            raise RuntimeError("T02 not found (is the Phomemo app still connected?)")
        self.c = BleakClient(dev)
        await self.c.connect()
        await self.c.start_notify(NOTIFY, self._on_notify)
        for q in INIT_QUERIES:
            await self._write(bytes([0x1F, 0x11, q]))
        await asyncio.sleep(1)  # let the replies (packet limit etc.) arrive
        return self

    async def __aexit__(self, *a):
        await self.c.disconnect()

    def _on_notify(self, _, b: bytes):
        if b[0] == 0x01 and len(b) >= 2:
            self.credits += b[1]; self._ev.set()
        elif b[0] == 0x02 and len(b) >= 2:
            self.limit = min(b[1], self.c.services.get_characteristic(WRITE).max_write_without_response_size)
        else:
            self.status.append(STATUS.get(bytes(b), b.hex()))
            if bytes(b) == bytes.fromhex("1a0f0c"): self._done.set()

    async def _write(self, chunk: bytes):
        while self.credits <= 0:  # ponytail: no timeout, add one if printer stalls
            self._ev.clear(); await asyncio.wait_for(self._ev.wait(), 8)
        self.credits -= 1
        await self.c.write_gatt_char(WRITE, chunk, response=False)

    async def send(self, data: bytes):
        for i in range(0, len(data), self.limit):
            await self._write(data[i:i + self.limit])

    async def print_image(self, img: Image.Image, feed: int = 4, block: int = 65535, **kw) -> bool:
        """True if the printer reported completion within 20s. kw: darkness, autocontrast."""
        self._done.clear()
        await self.send(job_bytes(img, feed, block, **kw))
        try:
            await asyncio.wait_for(self._done.wait(), 20); return True
        except asyncio.TimeoutError:
            return False


if __name__ == "__main__":  # python t02.py [image.png]
    import sys
    from PIL import ImageDraw
    if len(sys.argv) > 1:
        im = Image.open(sys.argv[1])
    else:
        im = Image.new("L", (WIDTH, 120), 255); d = ImageDraw.Draw(im)
        d.rectangle((0, 0, WIDTH - 1, 119), outline=0, width=3); d.text((20, 50), "T02 SDK test", fill=0)
    async def main():
        async with T02() as p:
            print("limit", p.limit, "done:", await p.print_image(im), p.status)
    asyncio.run(main())
