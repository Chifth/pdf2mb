"""
Universal PDF resource analyzer for project `2mb`.
Usage:
    python scripts/analyze_pdf.py <input.pdf> [--json report.json]
"""
import argparse
import io
import json
import os
import fitz
import pikepdf
from PIL import Image


def analyze_pdf(pdf_path: str) -> dict:
    file_bytes = os.path.getsize(pdf_path)
    doc = fitz.open(pdf_path)
    meta = dict(doc.metadata or {})
    page_count = len(doc)

    pages_info = []
    total_ocr_chars = 0
    total_images = 0
    sample_pixel_stats = None

    for idx, page in enumerate(doc):
        rect = page.rect
        imgs = page.get_images(full=True)
        fonts = page.get_fonts(full=True)
        text_len = len(page.get_text("text"))
        total_ocr_chars += text_len
        total_images += len(imgs)

        page_imgs = []
        for img in imgs:
            xref, smask, w, h, bpc, cs, alt_cs, name, inv_filter, _ = img
            rects = page.get_image_rects(xref)
            eff_dpi_x = eff_dpi_y = 0.0
            if rects and rects[0].width > 0 and rects[0].height > 0:
                eff_dpi_x = round(w / (rects[0].width / 72.0), 1)
                eff_dpi_y = round(h / (rects[0].height / 72.0), 1)
            page_imgs.append({
                "xref": xref,
                "smask": smask,
                "width": w,
                "height": h,
                "bpc": bpc,
                "colorspace": cs,
                "filter": inv_filter,
                "eff_dpi": [eff_dpi_x, eff_dpi_y],
            })

            if idx == 0 and sample_pixel_stats is None and w >= 800 and h >= 800:
                base = doc.extract_image(xref)
                if base:
                    im = Image.open(io.BytesIO(base["image"])).convert("RGB")
                    hsv = im.convert("HSV")
                    s_band = list(hsv.getdata(1))
                    total_px = im.width * im.height
                    high_s = sum(1 for x in s_band if x > 40) / total_px * 100.0
                    gray = im.convert("L")
                    hist = gray.histogram()
                    sample_pixel_stats = {
                        "page": 1,
                        "size_px": [im.width, im.height],
                        "mp": round(total_px / 1_000_000.0, 2),
                        "color_px_pct_s40": round(high_s, 2),
                        "lum_lt100_pct": round(sum(hist[:100]) / total_px * 100.0, 2),
                        "lum_100_200_pct": round(sum(hist[100:200]) / total_px * 100.0, 2),
                        "lum_gt200_pct": round(sum(hist[200:]) / total_px * 100.0, 2),
                        "lum_gt230_pct": round(sum(hist[230:]) / total_px * 100.0, 2),
                        "lum_gt245_pct": round(sum(hist[245:]) / total_px * 100.0, 2),
                    }

        pages_info.append({
            "page": idx + 1,
            "size_pt": [round(rect.width, 1), round(rect.height, 1)],
            "images_count": len(imgs),
            "fonts_count": len(fonts),
            "text_chars": text_len,
            "images": page_imgs,
        })
    doc.close()

    # Stream breakdown via pikepdf
    by_type = {}
    with pikepdf.open(pdf_path) as pdf:
        pdf_version = str(pdf.pdf_version)
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                sz = len(obj.read_raw_bytes())
                st = str(obj.get("/Subtype", "Content/Other"))
                flt = str(obj.get("/Filter", "None"))
                key = f"{st} ({flt})"
                by_type[key] = by_type.get(key, 0) + sz

    by_type_kib = {k: round(v / 1024.0, 1) for k, v in sorted(by_type.items(), key=lambda x: -x[1])}

    return {
        "file_path": os.path.abspath(pdf_path),
        "file_bytes": file_bytes,
        "file_kib": round(file_bytes / 1024.0, 1),
        "file_kb1000": round(file_bytes / 1000.0, 1),
        "pdf_version": pdf_version,
        "metadata": meta,
        "pages": page_count,
        "total_images": total_images,
        "total_text_chars": total_ocr_chars,
        "streams_kib": by_type_kib,
        "sample_pixel_stats": sample_pixel_stats,
        "pages_detail": pages_info,
    }


def main():
    parser = argparse.ArgumentParser(description="Analyze PDF structure & resource sizes.")
    parser.add_argument("pdf", help="Path to input PDF")
    parser.add_argument("--json", dest="json_out", help="Optional path to save JSON report")
    args = parser.parse_args()

    res = analyze_pdf(args.pdf)
    print(f"File: {res['file_path']}")
    print(f"Size: {res['file_bytes']:,} bytes ({res['file_kib']} KiB / {res['file_kb1000']} KB)")
    print(f"PDF {res['pdf_version']} | Pages: {res['pages']} | Images: {res['total_images']} | Text chars: {res['total_text_chars']}")
    print(f"Producer: {res['metadata'].get('producer', '')} | Creator: {res['metadata'].get('creator', '')}")
    print("\nStreams breakdown (KiB):")
    for k, v in res["streams_kib"].items():
        print(f"  {v:8.1f} KiB | {k}")
    if res["sample_pixel_stats"]:
        print("\nPage 1 main image pixel stats:")
        for k, v in res["sample_pixel_stats"].items():
            print(f"  {k}: {v}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"\nJSON saved to: {args.json_out}")


if __name__ == "__main__":
    main()

