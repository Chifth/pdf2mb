"""
PDF <= 2000 KB optimizer & 10-variant benchmark for project `2mb`.
Saves all artifacts to `.agents/worklog/YYYY-MM-DD[_slug]/` (or custom `--out-dir`).

Usage:
    python scripts/compress_2mb.py <input.pdf> [--slug name] [--out-dir path] [--max-bytes 1995000]
"""
import argparse
import datetime
import io
import json
import os
import re
import time

import fitz
import numpy as np
import pikepdf
from PIL import Image, ImageFilter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKLOG_ROOT = os.path.join(PROJECT_ROOT, ".agents", "worklog")


def clean_bg(im: Image.Image, mode: str = "smart", white_pt: int = 225, black_pt: int = 15, sat_thresh: int = 28) -> Image.Image:
    if mode == "raw":
        return im
    arr = np.asarray(im, dtype=np.float32)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    if mode == "gray":
        g_clean = np.clip((lum - black_pt) * (255.0 / (white_pt - black_pt)), 0, 255).astype(np.uint8)
        return Image.fromarray(g_clean)
    chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    low_s = sat_thresh * 0.55
    color_w = np.clip((chroma - low_s) / (sat_thresh - low_s), 0.0, 1.0)
    lum_clean = np.clip((lum - black_pt) * (255.0 / (white_pt - black_pt)), 0.0, 255.0)
    col_white = min(255.0, white_pt + 15.0)
    r_c = np.clip((r - black_pt * 0.5) * (255.0 / (col_white - black_pt * 0.5)), 0.0, 255.0)
    g_c = np.clip((g - black_pt * 0.5) * (255.0 / (col_white - black_pt * 0.5)), 0.0, 255.0)
    b_c = np.clip((b - black_pt * 0.5) * (255.0 / (col_white - black_pt * 0.5)), 0.0, 255.0)
    out = np.stack([
        lum_clean * (1.0 - color_w) + r_c * color_w,
        lum_clean * (1.0 - color_w) + g_c * color_w,
        lum_clean * (1.0 - color_w) + b_c * color_w,
    ], axis=-1).astype(np.uint8)
    return Image.fromarray(out)


def encode_g4(mask_pil_1bit: Image.Image) -> bytes:
    w, h = mask_pil_1bit.size
    buf = io.BytesIO()
    mask_pil_1bit.save(buf, format="TIFF", compression="group4", tiffinfo={278: h})
    buf.seek(0)
    tif = Image.open(buf)
    offsets = tif.tag_v2[273]
    counts = tif.tag_v2[279]
    data = buf.getvalue()
    if len(offsets) == 1:
        return data[offsets[0]:offsets[0] + counts[0]]
    return b"".join(data[o:o + c] for o, c in zip(offsets, counts))


def _make_bg_xobj(pdf, bg_im, bg_format, bg_param, subsampling):
    buf = io.BytesIO()
    if bg_format == "JPEG":
        bg_im.save(buf, format="JPEG", quality=bg_param, optimize=True, subsampling=subsampling)
        flt = "/DCTDecode"
    else:
        bg_im.save(buf, format="JPEG2000", quality_mode="rates", quality_layers=[bg_param])
        flt = "/JPXDecode"
    xobj = pikepdf.Stream(pdf, buf.getvalue())
    xobj["/Type"] = pikepdf.Name("/XObject")
    xobj["/Subtype"] = pikepdf.Name("/Image")
    xobj["/Width"] = bg_im.width
    xobj["/Height"] = bg_im.height
    xobj["/BitsPerComponent"] = 8
    xobj["/ColorSpace"] = pikepdf.Name("/DeviceRGB")
    xobj["/Filter"] = pikepdf.Name(flt)
    return xobj


def _make_mask_xobj(pdf, g4_bytes, mw, mh):
    mask = pikepdf.Stream(pdf, g4_bytes)
    mask["/Type"] = pikepdf.Name("/XObject")
    mask["/Subtype"] = pikepdf.Name("/Image")
    mask["/Width"] = mw
    mask["/Height"] = mh
    mask["/BitsPerComponent"] = 1
    mask["/ImageMask"] = True
    mask["/Filter"] = pikepdf.Name("/CCITTFaxDecode")
    mask["/DecodeParms"] = pikepdf.Dictionary({
        "/K": -1, "/Columns": mw, "/Rows": mh, "/BlackIs1": False
    })
    return mask


def measure_layers_kib(pdf_bytes: bytes) -> dict:
    by_flt = {}
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                sz = len(obj.read_raw_bytes())
                flt = str(obj.get("/Filter", "None"))
                st = str(obj.get("/Subtype", "Content/OCR"))
                k = f"{st} ({flt})"
                by_flt[k] = by_flt.get(k, 0) + sz
    return {k: round(v / 1024.0, 1) for k, v in sorted(by_flt.items(), key=lambda x: -x[1])}


def binsearch_param(build_fn, lo, hi, target_max_bytes, higher_is_bigger=True):
    best_p, best_b = None, None
    while lo <= hi:
        mid = (lo + hi) // 2
        b = build_fn(mid)
        if len(b) <= target_max_bytes:
            best_p, best_b = mid, b
            if higher_is_bigger:
                lo = mid + 1
            else:
                hi = mid - 1
        else:
            if higher_is_bigger:
                hi = mid - 1
            else:
                lo = mid + 1
    if best_b is None:
        p = lo if higher_is_bigger else hi
        best_p, best_b = p, build_fn(p)
    return best_p, best_b


def run_benchmark(pdf_path: str, out_dir: str, max_bytes: int = 1_995_000):
    variants_dir = os.path.join(out_dir, "variants")
    crops_dir = os.path.join(out_dir, "crops")
    os.makedirs(variants_dir, exist_ok=True)
    os.makedirs(crops_dir, exist_ok=True)

    src_doc = fitz.open(pdf_path)
    page_rects = [p.rect for p in src_doc]
    orig_bytes = os.path.getsize(pdf_path)
    n_pages = len(src_doc)

    render_cache = {}
    def get_pages(dpi):
        if dpi not in render_cache:
            zoom = dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            imgs = []
            for p in src_doc:
                pix = p.get_pixmap(matrix=mat, alpha=False)
                imgs.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
            render_cache[dpi] = imgs
        return render_cache[dpi]

    def build_jpeg_pdf(images, quality, subsampling=2):
        out_doc = fitz.open()
        for im, rect in zip(images, page_rects):
            buf = io.BytesIO()
            kw = {"format": "JPEG", "quality": quality, "optimize": True}
            if im.mode == "RGB":
                kw["subsampling"] = subsampling
            im.save(buf, **kw)
            page = out_doc.new_page(width=rect.width, height=rect.height)
            page.insert_image(rect, stream=buf.getvalue())
        b = out_doc.tobytes(garbage=4, deflate=True, clean=True)
        out_doc.close()
        return b

    def build_jp2_pdf(images, rate):
        pdf = pikepdf.new()
        for im, rect in zip(images, page_rects):
            buf = io.BytesIO()
            im.save(buf, format="JPEG2000", quality_mode="rates", quality_layers=[rate])
            page = pdf.add_blank_page(page_size=(rect.width, rect.height))
            xobj = pikepdf.Stream(pdf, buf.getvalue())
            xobj["/Type"] = pikepdf.Name("/XObject")
            xobj["/Subtype"] = pikepdf.Name("/Image")
            xobj["/Width"] = im.width
            xobj["/Height"] = im.height
            xobj["/BitsPerComponent"] = 8
            xobj["/ColorSpace"] = pikepdf.Name("/DeviceGray" if im.mode == "L" else "/DeviceRGB")
            xobj["/Filter"] = pikepdf.Name("/JPXDecode")
            page["/Resources"] = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/Im0": xobj})})
            cmd = f"q {rect.width:.4f} 0 0 {rect.height:.4f} 0 0 cm /Im0 Do Q".encode("ascii")
            page["/Contents"] = pikepdf.Stream(pdf, cmd)
        out = io.BytesIO()
        pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        return out.getvalue()

    def prepare_mrc(dpi_mask=300, dpi_bg=150, text_lum_thresh=165, sat_thresh=30, bg_white_pt=225):
        imgs_hi = get_pages(dpi_mask)
        masks_g4, bg_imgs = [], []
        for im in imgs_hi:
            arr = np.asarray(im, dtype=np.float32)
            r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
            is_text = (lum < text_lum_thresh) & (chroma < sat_thresh)
            mask_arr = np.where(is_text, 255, 0).astype(np.uint8)
            g4_bytes = encode_g4(Image.fromarray(mask_arr).convert("1", dither=Image.Dither.NONE))
            masks_g4.append((g4_bytes, im.width, im.height))

            dil_pil = Image.fromarray(mask_arr).filter(ImageFilter.MaxFilter(3))
            erase_final = (np.asarray(dil_pil) > 0) & (chroma < sat_thresh)

            low_s = sat_thresh * 0.55
            color_w = np.clip((chroma - low_s) / (sat_thresh - low_s), 0.0, 1.0)
            lum_clean = np.clip((lum - 15.0) * (255.0 / (bg_white_pt - 15.0)), 0.0, 255.0)
            col_white = min(255.0, bg_white_pt + 15.0)
            r_c = np.clip((r - 8.0) * (255.0 / (col_white - 8.0)), 0.0, 255.0)
            g_c = np.clip((g - 8.0) * (255.0 / (col_white - 8.0)), 0.0, 255.0)
            b_c = np.clip((b - 8.0) * (255.0 / (col_white - 8.0)), 0.0, 255.0)
            bg_r = np.where(erase_final, 255.0, lum_clean * (1.0 - color_w) + r_c * color_w)
            bg_g = np.where(erase_final, 255.0, lum_clean * (1.0 - color_w) + g_c * color_w)
            bg_b = np.where(erase_final, 255.0, lum_clean * (1.0 - color_w) + b_c * color_w)
            bg_pil = Image.fromarray(np.stack([bg_r, bg_g, bg_b], axis=-1).astype(np.uint8))
            if dpi_bg != dpi_mask:
                bg_pil = bg_pil.resize((
                    max(1, int(round(im.width * dpi_bg / dpi_mask))),
                    max(1, int(round(im.height * dpi_bg / dpi_mask))),
                ), Image.Resampling.LANCZOS)
            bg_imgs.append(bg_pil)
        return masks_g4, bg_imgs

    def build_mrc_pdf(masks_g4, bg_imgs, bg_format="JPEG", bg_param=75, subsampling=2, keep_ocr=False):
        if keep_ocr:
            pdf = pikepdf.open(pdf_path)
            for page, (g4_bytes, mw, mh), bg_im, rect in zip(pdf.pages, masks_g4, bg_imgs, page_rects):
                raw_content = page.Contents.read_bytes() if not isinstance(page.Contents, pikepdf.Array) else b"".join(s.read_bytes() for s in page.Contents)
                old_xobjs = list(page.get("/Resources", {}).get("/XObject", {}).keys())
                content_str = raw_content.decode("latin1")
                for xname in old_xobjs:
                    content_str = re.sub(re.escape(xname) + r"\s+Do\b", "", content_str)
                bg_xobj = _make_bg_xobj(pdf, bg_im, bg_format, bg_param, subsampling)
                mask_xobj = _make_mask_xobj(pdf, g4_bytes, mw, mh)
                page["/Resources"]["/XObject"] = pikepdf.Dictionary({"/MrcBg": bg_xobj, "/MrcMask": mask_xobj})
                draw_cmd = (
                    f"\nq {rect.width:.4f} 0 0 {rect.height:.4f} 0 0 cm /MrcBg Do Q\n"
                    f"q 0.08 0.08 0.08 rg {rect.width:.4f} 0 0 {rect.height:.4f} 0 0 cm /MrcMask Do Q\n"
                )
                page["/Contents"] = pikepdf.Stream(pdf, (content_str + draw_cmd).encode("latin1"))
            out = io.BytesIO()
            pdf.remove_unreferenced_resources()
            pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
            pdf.close()
            return out.getvalue()
        pdf = pikepdf.new()
        for (g4_bytes, mw, mh), bg_im, rect in zip(masks_g4, bg_imgs, page_rects):
            page = pdf.add_blank_page(page_size=(rect.width, rect.height))
            bg_xobj = _make_bg_xobj(pdf, bg_im, bg_format, bg_param, subsampling)
            mask_xobj = _make_mask_xobj(pdf, g4_bytes, mw, mh)
            page["/Resources"] = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/MrcBg": bg_xobj, "/MrcMask": mask_xobj})})
            cmd = (
                f"q {rect.width:.4f} 0 0 {rect.height:.4f} 0 0 cm /MrcBg Do Q\n"
                f"q 0.08 0.08 0.08 rg {rect.width:.4f} 0 0 {rect.height:.4f} 0 0 cm /MrcMask Do Q"
            ).encode("ascii")
            page["/Contents"] = pikepdf.Stream(pdf, cmd)
        out = io.BytesIO()
        pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        return out.getvalue()

    def save_crop(pdf_bytes, tag):
        d = fitz.open(stream=pdf_bytes, filetype="pdf")
        mat = fitz.Matrix(300 / 72.0, 300 / 72.0)
        p1 = d[0].get_pixmap(matrix=mat, alpha=False)
        im1 = Image.frombytes("RGB", [p1.width, p1.height], p1.samples)
        w, h = im1.size
        im1.crop((int(w * 0.12), int(h * 0.17), min(w, int(w * 0.12) + 1000), min(h, int(h * 0.17) + 600))).save(
            os.path.join(crops_dir, f"{tag}_p1.png")
        )
        d.close()

    results = []
    print(f"{'ID':<30} | {'Param':<14} | {'KiB':>7} | {'KB(1000)':>8} | {'Ratio':>6} | {'Time':>5}")
    print("-" * 88)

    def run_v(vid, dpi_eff, fn):
        t0 = time.time()
        param_str, pdf_bytes = fn()
        dt = time.time() - t0
        out_path = os.path.join(variants_dir, f"{vid}.pdf")
        with open(out_path, "wb") as f:
            f.write(pdf_bytes)
        save_crop(pdf_bytes, vid)
        kib = len(pdf_bytes) / 1024.0
        kb1000 = len(pdf_bytes) / 1000.0
        ratio = orig_bytes / len(pdf_bytes) if pdf_bytes else 0
        layers = measure_layers_kib(pdf_bytes)
        total_mp = sum((r.width / 72.0 * dpi_eff) * (r.height / 72.0 * dpi_eff) / 1_000_000.0 for r in page_rects)
        kib_per_mp = round(kib / total_mp, 2) if total_mp > 0 else 0
        print(f"{vid:<30} | {param_str:<14} | {kib:>7.1f} | {kb1000:>8.1f} | {ratio:>5.2f}x | {dt:>4.1f}s", flush=True)
        results.append({
            "id": vid,
            "param": param_str,
            "bytes": len(pdf_bytes),
            "kib": round(kib, 1),
            "kb1000": round(kb1000, 1),
            "compression_ratio": round(ratio, 2),
            "total_mp": round(total_mp, 2),
            "kib_per_mp": kib_per_mp,
            "layers_kib": layers,
            "file": out_path,
        })

    run_v("V01_jpeg_150dpi_raw", 150, lambda: (lambda p, b: (f"Q={p}", b))(*binsearch_param(lambda q: build_jpeg_pdf(get_pages(150), q, 2), 15, 92, max_bytes, True)))
    imgs_165 = [clean_bg(im, "smart", 225, 15, 28) for im in get_pages(165)]
    run_v("V02_jpeg_165dpi_smart", 165, lambda: (lambda p, b: (f"Q={p}", b))(*binsearch_param(lambda q: build_jpeg_pdf(imgs_165, q, 2), 15, 92, max_bytes, True)))
    imgs_180_g = [clean_bg(im, "gray", 225, 15) for im in get_pages(180)]
    run_v("V03_jpeg_180dpi_gray", 180, lambda: (lambda p, b: (f"Q={p}", b))(*binsearch_param(lambda q: build_jpeg_pdf(imgs_180_g, q, 2), 15, 92, max_bytes, True)))
    run_v("V04_jp2_200dpi_raw", 200, lambda: (lambda p, b: (f"rate={p}", b))(*binsearch_param(lambda r: build_jp2_pdf(get_pages(200), r), 10, 160, max_bytes, False)))
    imgs_250 = [clean_bg(im, "smart", 225, 15, 28) for im in get_pages(250)]
    run_v("V05_jp2_250dpi_smart", 250, lambda: (lambda p, b: (f"rate={p}", b))(*binsearch_param(lambda r: build_jp2_pdf(imgs_250, r), 15, 220, max_bytes, False)))
    imgs_300 = [clean_bg(im, "smart", 225, 15, 28) for im in get_pages(300)]
    run_v("V06_jp2_300dpi_smart", 300, lambda: (lambda p, b: (f"rate={p}", b))(*binsearch_param(lambda r: build_jp2_pdf(imgs_300, r), 20, 300, max_bytes, False)))
    imgs_300_g = [clean_bg(im, "gray", 225, 15) for im in get_pages(300)]
    run_v("V07_jp2_300dpi_gray", 300, lambda: (lambda p, b: (f"rate={p}", b))(*binsearch_param(lambda r: build_jp2_pdf(imgs_300_g, r), 10, 160, max_bytes, False)))

    m165, bg150 = prepare_mrc(300, 150, 165, 30, 225)
    run_v("V08_mrc_300g4_150jpg", 300, lambda: (lambda p, b: (f"bgQ={p}", b))(*binsearch_param(lambda q: build_mrc_pdf(m165, bg150, "JPEG", q, 2, False), 20, 95, max_bytes, True)))
    run_v("V09_mrc_300g4_150jpg_OCR", 300, lambda: (lambda p, b: (f"bgQ={p}+OCR", b))(*binsearch_param(lambda q: build_mrc_pdf(m165, bg150, "JPEG", q, 2, True), 20, 95, max_bytes, True)))
    m175, bg200 = prepare_mrc(300, 200, 175, 30, 225)
    run_v("V10_mrc_300g4_200jpg_OCR", 300, lambda: (lambda p, b: (f"bgQ={p}+OCR", b))(*binsearch_param(lambda q: build_mrc_pdf(m175, bg200, "JPEG", q, 2, True), 20, 95, max_bytes, True)))

    report = {
        "input_pdf": os.path.abspath(pdf_path),
        "orig_bytes": orig_bytes,
        "pages": n_pages,
        "target_max_bytes": max_bytes,
        "variants": results,
    }
    report_path = os.path.join(out_dir, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nReport saved to: {report_path}")
    src_doc.close()


def main():
    parser = argparse.ArgumentParser(description="Compress PDF to <= 2000 KB using up to 10 strategies.")
    parser.add_argument("pdf", help="Path to input PDF")
    parser.add_argument("--slug", default="", help="Optional slug suffix for date folder")
    parser.add_argument("--out-dir", default="", help="Optional explicit output directory (e.g. next to source files)")
    parser.add_argument("--max-bytes", type=int, default=1_995_000, help="Target max size in bytes (default: 1995000)")
    args = parser.parse_args()

    if args.out_dir:
        out_dir = os.path.abspath(args.out_dir)
    else:
        today = datetime.date.today().strftime("%Y-%m-%d")
        folder = f"{today}_{args.slug}" if args.slug else today
        out_dir = os.path.join(WORKLOG_ROOT, folder)
    os.makedirs(out_dir, exist_ok=True)
    run_benchmark(args.pdf, out_dir, max_bytes=args.max_bytes)


if __name__ == "__main__":
    main()

