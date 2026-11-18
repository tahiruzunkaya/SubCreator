"""assets/icon.ico dosyasını üretir (uygulama içi simgeyle aynı: mavi kare üstünde 'TR')."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = max(1, size // 32)
    d.rounded_rectangle([m, m, size - m - 1, size - m - 1], radius=int(size * 0.2), fill=(0, 120, 215, 255))
    font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", max(6, int(size * 0.47)))
    d.text((size / 2, size / 2), "TR", font=font, fill="white", anchor="mm")
    return img


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    draw(256).save(OUT, sizes=[(s, s) for s in sizes])
    print(f"Yazıldı: {OUT}")


if __name__ == "__main__":
    main()
