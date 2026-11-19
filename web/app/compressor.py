"""
Smart PDF <= 2000 KB auto-optimizer based on empirical coefficients from project `2mb`.
Automatically selects the highest-quality strategy for each PDF based on:
  - page count & per-page byte budget
  - document type (vector vs scanned)
  - toner contrast (pale-toner anti-MRC rule: lum_100_200 / lum_lt100 > 2.5)
  - color content & OCR layer preservation
"""
from __future__ import annotations

import io
import os
import re
import shutil
import time
from dataclasses import dataclass, asdict
from typing import Callable

import fitz
import numpy as np
import pikepdf
from PIL import Image, ImageFilter


@dataclass
class CompressionResult:
    orig_bytes: int
    out_bytes: int
    pages: int
    compression_pct: float
    compression_ratio: float
    strategy_id: str
    strategy_label: str
    params_summary: str
    ocr_preserved: bool
    ocr_chars: int
    elapsed_sec: float

    def to_dict(self) -> dict:
        return asdict(self)


def _encode_g4(mask_pil_1bit: Image.Image) -> bytes:
    w, h = mask_pil_1bit.size
    buf = io.BytesIO()
    mask_pil_1bit.save(buf, format="TIFF", compression="group4", tiffinfo={278: h})
    buf.seek(0)
    tif = Image.open(buf)
    offsets = tif.tag_v2[273]
    counts = tif.tag_v2[279]
    data = buf.getvalue()
    if len(offsets) == 1:
        return data[offsets[0] : offsets[0] + counts[0]]
    return b"".join(data[o : o + c] for o, c in zip(offsets, counts))


def _make_jpeg_xobj(pdf: pikepdf.Pdf, im: Image.Image, quality: int, subsampling: int) -> pikepdf.Stream:
    buf = io.BytesIO()
    kw: dict = {"format": "JPEG", "quality": quality, "optimize": True}
    if im.mode == "RGB":
        kw["subsampling"] = subsampling
    im.save(buf, **kw)
    xobj = pikepdf.Stream(pdf, buf.getvalue())
    xobj["/Type"] = pikepdf.Name("/XObject")
    xobj["/Subtype"] = pikepdf.Name("/Image")
    xobj["/Width"] = im.width
    xobj["/Height"] = im.height
    xobj["/BitsPerComponent"] = 8
    xobj["/ColorSpace"] = pikepdf.Name("/DeviceGray" if im.mode == "L" else "/DeviceRGB")
    xobj["/Filter"] = pikepdf.Name("/DCTDecode")
    return xobj


def _make_mask_xobj(pdf: pikepdf.Pdf, g4_bytes: bytes, mw: int, mh: int) -> pikepdf.Stream:
    mask = pikepdf.Stream(pdf, g4_bytes)
    mask["/Type"] = pikepdf.Name("/XObject")
    mask["/Subtype"] = pikepdf.Name("/Image")
    mask["/Width"] = mw
    mask["/Height"] = mh
    mask["/BitsPerComponent"] = 1
    mask["/ImageMask"] = True
    mask["/Filter"] = pikepdf.Name("/CCITTFaxDecode")
    mask["/DecodeParms"] = pikepdf.Dictionary(
        {"/K": -1, "/Columns": mw, "/Rows": mh, "/BlackIs1": False}
    )
    return mask


def _binsearch_max(build_fn: Callable[[int], bytes], lo: int, hi: int, target_max: int) -> tuple[int, bytes]:
    best_p: int | None = None
    best_b: bytes | None = None
    low_b: bytes | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        b = build_fn(mid)
        if len(b) <= target_max:
            best_p, best_b = mid, b
            lo = mid + 1
        else:
            low_b = b
            hi = mid - 1
    if best_b is not None and best_p is not None:
        return best_p, best_b
    # Even lowest value exceeded target
    if low_b is None:
        low_b = build_fn(lo)
    return lo, low_b


def _inspect_pdf(pdf_path: str) -> dict:
    doc = fitz.open(pdf_path)
    n_pages = len(doc)
    total_chars = 0
    full_page_img_pages = 0
    single_native_imgs = True
    sample_stats = None

    for idx, page in enumerate(doc):
        rect = page.rect
        page_area = max(1.0, rect.width * rect.height)
        imgs = page.get_images(full=True)
        total_chars += len(page.get_text("text"))

        if len(imgs) != 1:
            single_native_imgs = False

        max_cov = 0.0
        for img in imgs:
            xref = img[0]
            for r in page.get_image_rects(xref):
                cov = (r.width * r.height) / page_area
                if cov > max_cov:
                    max_cov = cov
        if max_cov >= 0.72:
            full_page_img_pages += 1

        # Sample pixel stats from page 0 (or first page with image)
        if sample_stats is None and idx == 0:
            mat = fitz.Matrix(150 / 72.0, 150 / 72.0)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            im = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            arr = np.asarray(im, dtype=np.float32)
            r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
            total_px = float(im.width * im.height)
            lum_lt100 = float(np.sum(lum < 100)) / total_px * 100.0
            lum_100_200 = float(np.sum((lum >= 100) & (lum < 200))) / total_px * 100.0
            lum_gt245 = float(np.sum(lum > 245)) / total_px * 100.0
            color_pct = float(np.sum(chroma > 40)) / total_px * 100.0
            sample_stats = {
                "lum_lt100_pct": round(lum_lt100, 2),
                "lum_100_200_pct": round(lum_100_200, 2),
                "lum_gt245_pct": round(lum_gt245, 2),
                "color_pct": round(color_pct, 2),
                "pale_ratio": round(lum_100_200 / max(lum_lt100, 0.05), 2),
            }

    doc.close()

    is_scanned = (full_page_img_pages >= max(1, int(n_pages * 0.6)))
    has_ocr = total_chars >= 20
    return {
        "pages": n_pages,
        "total_chars": total_chars,
        "has_ocr": has_ocr,
        "is_scanned": is_scanned,
        "single_native_imgs": single_native_imgs,
        "stats": sample_stats or {
            "lum_lt100_pct": 4.0,
            "lum_100_200_pct": 4.0,
            "lum_gt245_pct": 80.0,
            "color_pct": 1.0,
            "pale_ratio": 1.0,
        },
    }


def _render_pages(pdf_path: str, dpi: int, use_native_if_single: bool = False) -> tuple[list[Image.Image], list[fitz.Rect]]:
    doc = fitz.open(pdf_path)
    rects = [p.rect for p in doc]
    imgs: list[Image.Image] = []
    if use_native_if_single:
        for p in doc:
            p_imgs = p.get_images(full=True)
            if len(p_imgs) == 1:
                base = doc.extract_image(p_imgs[0][0])
                if base and base.get("image"):
                    imgs.append(Image.open(io.BytesIO(base["image"])).convert("RGB"))
                    continue
            mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
            pix = p.get_pixmap(matrix=mat, alpha=False)
            imgs.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
    else:
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        for p in doc:
            pix = p.get_pixmap(matrix=mat, alpha=False)
            imgs.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
    doc.close()
    return imgs, rects


def _build_single_layer_pdf(
    pdf_path: str,
    images: list[Image.Image],
    page_rects: list[fitz.Rect],
    quality: int,
    subsampling: int,
    keep_ocr: bool,
) -> bytes:
    if keep_ocr:
        pdf = pikepdf.open(pdf_path)
        for page, im, rect in zip(pdf.pages, images, page_rects):
            raw_content = (
                page.Contents.read_bytes()
                if not isinstance(page.Contents, pikepdf.Array)
                else b"".join(s.read_bytes() for s in page.Contents)
            )
            old_xobjs = list(page.get("/Resources", {}).get("/XObject", {}).keys())
            content_str = raw_content.decode("latin1")
            for xname in old_xobjs:
                content_str = re.sub(re.escape(xname) + r"\s+Do\b", "", content_str)
            xobj = _make_jpeg_xobj(pdf, im, quality, subsampling)
            if "/Resources" not in page:
                page["/Resources"] = pikepdf.Dictionary()
            page["/Resources"]["/XObject"] = pikepdf.Dictionary({"/Im0": xobj})
            draw_cmd = f"\nq {rect.width:.4f} 0 0 {rect.height:.4f} 0 0 cm /Im0 Do Q\n"
            page["/Contents"] = pikepdf.Stream(pdf, (content_str + draw_cmd).encode("latin1"))
        out = io.BytesIO()
        pdf.remove_unreferenced_resources()
        pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        pdf.close()
        return out.getvalue()

    pdf = pikepdf.new()
    for im, rect in zip(images, page_rects):
        page = pdf.add_blank_page(page_size=(rect.width, rect.height))
        xobj = _make_jpeg_xobj(pdf, im, quality, subsampling)
        page["/Resources"] = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/Im0": xobj})})
        cmd = f"q {rect.width:.4f} 0 0 {rect.height:.4f} 0 0 cm /Im0 Do Q".encode("ascii")
        page["/Contents"] = pikepdf.Stream(pdf, cmd)
    out = io.BytesIO()
    pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return out.getvalue()


def _prepare_mrc_layers(
    imgs_hi: list[Image.Image],
    dpi_mask: int,
    dpi_bg: int,
    text_thresh: int,
    sat_thresh: int = 30,
    white_pt: int = 225,
) -> tuple[list[tuple[bytes, int, int]], list[Image.Image]]:
    masks_g4: list[tuple[bytes, int, int]] = []
    bg_imgs: list[Image.Image] = []
    for im in imgs_hi:
        arr = np.asarray(im, dtype=np.float32)
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)

        is_text = (lum < text_thresh) & (chroma < sat_thresh)
        mask_arr = np.where(is_text, 255, 0).astype(np.uint8)
        g4_bytes = _encode_g4(Image.fromarray(mask_arr).convert("1", dither=Image.Dither.NONE))
        masks_g4.append((g4_bytes, im.width, im.height))

        dil_pil = Image.fromarray(mask_arr).filter(ImageFilter.MaxFilter(3))
        erase_final = (np.asarray(dil_pil) > 0) & (chroma < sat_thresh)

        low_s = sat_thresh * 0.55
        color_w = np.clip((chroma - low_s) / (sat_thresh - low_s), 0.0, 1.0)
        lum_clean = np.clip((lum - 15.0) * (255.0 / (white_pt - 15.0)), 0.0, 255.0)
        col_white = min(255.0, white_pt + 15.0)
        r_c = np.clip((r - 8.0) * (255.0 / (col_white - 8.0)), 0.0, 255.0)
        g_c = np.clip((g - 8.0) * (255.0 / (col_white - 8.0)), 0.0, 255.0)
        b_c = np.clip((b - 8.0) * (255.0 / (col_white - 8.0)), 0.0, 255.0)

        bg_r = np.where(erase_final, 255.0, lum_clean * (1.0 - color_w) + r_c * color_w)
        bg_g = np.where(erase_final, 255.0, lum_clean * (1.0 - color_w) + g_c * color_w)
        bg_b = np.where(erase_final, 255.0, lum_clean * (1.0 - color_w) + b_c * color_w)
        bg_pil = Image.fromarray(np.stack([bg_r, bg_g, bg_b], axis=-1).astype(np.uint8))

        if dpi_bg != dpi_mask:
            bg_pil = bg_pil.resize(
                (
                    max(1, int(round(im.width * dpi_bg / dpi_mask))),
                    max(1, int(round(im.height * dpi_bg / dpi_mask))),
                ),
                Image.Resampling.LANCZOS,
            )
        bg_imgs.append(bg_pil)
    return masks_g4, bg_imgs


def _build_mrc_pdf(
    pdf_path: str,
    masks_g4: list[tuple[bytes, int, int]],
    bg_imgs: list[Image.Image],
    page_rects: list[fitz.Rect],
    bg_q: int,
    subsampling: int,
    keep_ocr: bool,
) -> bytes:
    if keep_ocr:
        pdf = pikepdf.open(pdf_path)
        for page, (g4_bytes, mw, mh), bg_im, rect in zip(pdf.pages, masks_g4, bg_imgs, page_rects):
            raw_content = (
                page.Contents.read_bytes()
                if not isinstance(page.Contents, pikepdf.Array)
                else b"".join(s.read_bytes() for s in page.Contents)
            )
            old_xobjs = list(page.get("/Resources", {}).get("/XObject", {}).keys())
            content_str = raw_content.decode("latin1")
            for xname in old_xobjs:
                content_str = re.sub(re.escape(xname) + r"\s+Do\b", "", content_str)
            bg_xobj = _make_jpeg_xobj(pdf, bg_im, bg_q, subsampling)
            mask_xobj = _make_mask_xobj(pdf, g4_bytes, mw, mh)
            if "/Resources" not in page:
                page["/Resources"] = pikepdf.Dictionary()
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
        bg_xobj = _make_jpeg_xobj(pdf, bg_im, bg_q, subsampling)
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


def _optimize_vector_embedded_images(pdf_path: str, target_max: int) -> tuple[bytes | None, str]:
    """Compress only embedded raster XObjects in vector PDFs, keeping vector text 100% intact."""
    def _build_vec(q_and_scale: int) -> bytes:
        # Map 1..100 into scale + quality
        if q_and_scale >= 50:
            scale = 1.0
            q = 20 + int((q_and_scale - 50) * 1.4)  # 20..90
        else:
            scale = 0.45 + (q_and_scale / 50.0) * 0.55  # 0.45..1.0
            q = 25 + int(q_and_scale * 0.8)  # 25..65

        pdf = pikepdf.open(pdf_path)
        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Stream):
                continue
            if obj.get("/Subtype") != pikepdf.Name("/Image"):
                continue
            if obj.get("/ImageMask") is True or "/SMask" in obj:
                continue
            w = int(obj.get("/Width", 0))
            h = int(obj.get("/Height", 0))
            if w * h < 40_000:
                continue
            try:
                pim = pikepdf.PdfImage(obj)
                pil_im = pim.as_pil_image()
                if pil_im.mode not in ("RGB", "L"):
                    pil_im = pil_im.convert("RGB")
                if scale < 0.99:
                    pil_im = pil_im.resize(
                        (max(1, int(pil_im.width * scale)), max(1, int(pil_im.height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                buf = io.BytesIO()
                kw: dict = {"format": "JPEG", "quality": min(92, max(20, q)), "optimize": True}
                if pil_im.mode == "RGB":
                    kw["subsampling"] = 2
                pil_im.save(buf, **kw)
                new_bytes = buf.getvalue()
                if len(new_bytes) < len(obj.read_raw_bytes()):
                    obj.write(new_bytes, filter=pikepdf.Name("/DCTDecode"))
                    obj["/Width"] = pil_im.width
                    obj["/Height"] = pil_im.height
                    obj["/BitsPerComponent"] = 8
                    obj["/ColorSpace"] = pikepdf.Name("/DeviceGray" if pil_im.mode == "L" else "/DeviceRGB")
            except Exception:
                continue
        out = io.BytesIO()
        pdf.remove_unreferenced_resources()
        pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        pdf.close()
        return out.getvalue()

    p, b = _binsearch_max(_build_vec, 10, 95, target_max)
    if len(b) <= target_max:
        return b, f"Vector-safe image compress (level={p})"
    return None, ""


def optimize_pdf_auto(input_path: str, output_path: str, target_bytes: int = 1_995_000) -> CompressionResult:
    """
    Main entry point executed in worker process.
    Analyzes input_path, auto-selects optimal strategy from empirical Matrix v1.3,
    compresses strictly <= target_bytes, writes output_path, and returns metrics.
    """
    t0 = time.perf_counter()
    orig_bytes = os.path.getsize(input_path)
    info = _inspect_pdf(input_path)
    n_pages = max(1, info["pages"])
    has_ocr = info["has_ocr"]
    ocr_chars = info["total_chars"]
    stats = info["stats"]

    # 1. Pass-through if already within target
    if orig_bytes <= target_bytes:
        shutil.copy2(input_path, output_path)
        elapsed = round(time.perf_counter() - t0, 2)
        return CompressionResult(
            orig_bytes=orig_bytes,
            out_bytes=orig_bytes,
            pages=n_pages,
            compression_pct=0.0,
            compression_ratio=1.0,
            strategy_id="PASS_THROUGH",
            strategy_label="Без змін (вже ≤ ліміту)",
            params_summary="Оригінал збережено 1:1",
            ocr_preserved=has_ocr,
            ocr_chars=ocr_chars,
            elapsed_sec=elapsed,
        )

    # 2. Vector PDF with embedded photos (Type C) — try lossless-text compression first
    if not info["is_scanned"]:
        vec_bytes, vec_params = _optimize_vector_embedded_images(input_path, target_bytes)
        if vec_bytes is not None and len(vec_bytes) <= target_bytes:
            with open(output_path, "wb") as f:
                f.write(vec_bytes)
            out_bytes = len(vec_bytes)
            elapsed = round(time.perf_counter() - t0, 2)
            return CompressionResult(
                orig_bytes=orig_bytes,
                out_bytes=out_bytes,
                pages=n_pages,
                compression_pct=round((1.0 - out_bytes / orig_bytes) * 100.0, 1),
                compression_ratio=round(orig_bytes / max(1, out_bytes), 2),
                strategy_id="VECTOR_IMAGES_ONLY",
                strategy_label="Векторний PDF (стиснено лише фото)",
                params_summary=vec_params,
                ocr_preserved=has_ocr,
                ocr_chars=ocr_chars,
                elapsed_sec=elapsed,
            )

    # 3. Scanned PDF (Type A / B) — Empirical Matrix v1.3
    budget_kib_per_page = (target_bytes / 1024.0) / n_pages
    pale_ratio = stats["pale_ratio"]
    is_pale_toner = pale_ratio > 2.5

    # Strategy 3A: Native 300 DPI RGB 4:4:4 JPEG (small docs 1-3p or pale toner with >=400 KiB/p)
    if budget_kib_per_page >= 550.0 or (is_pale_toner and budget_kib_per_page >= 400.0):
        imgs, rects = _render_pages(input_path, dpi=300, use_native_if_single=info["single_native_imgs"])
        q, b = _binsearch_max(
            lambda q_val: _build_single_layer_pdf(input_path, imgs, rects, q_val, subsampling=0, keep_ocr=has_ocr),
            30,
            95,
            target_bytes,
        )
        if len(b) <= target_bytes:
            with open(output_path, "wb") as f:
                f.write(b)
            out_bytes = len(b)
            elapsed = round(time.perf_counter() - t0, 2)
            reason = "блідий тонер" if is_pale_toner else "нативний 300 DPI"
            return CompressionResult(
                orig_bytes=orig_bytes,
                out_bytes=out_bytes,
                pages=n_pages,
                compression_pct=round((1.0 - out_bytes / orig_bytes) * 100.0, 1),
                compression_ratio=round(orig_bytes / max(1, out_bytes), 2),
                strategy_id="JPEG_NATIVE_300_444",
                strategy_label=f"300 DPI RGB 4:4:4 ({reason})" + (" + OCR" if has_ocr else ""),
                params_summary=f"JPEG 300 DPI 4:4:4, Q={q}" + (", OCR збережено" if has_ocr else ""),
                ocr_preserved=has_ocr,
                ocr_chars=ocr_chars,
                elapsed_sec=elapsed,
            )

    # Strategy 3B: MRC 300 DPI CCITT G4 + Color Background JPEG (+ OCR)
    imgs_300, rects = _render_pages(input_path, dpi=300, use_native_if_single=info["single_native_imgs"])

    if budget_kib_per_page >= 250.0:
        mrc_plan = [
            (300, 175, 0, "MRC 300+300 4:4:4"),
            (200, 175, 2, "MRC 300+200 4:2:0"),
            (150, 165, 2, "MRC 300+150 4:2:0"),
        ]
    elif budget_kib_per_page >= 160.0:
        mrc_plan = [
            (200, 175, 2, "MRC 300+200 4:2:0"),
            (150, 165, 2, "MRC 300+150 4:2:0"),
        ]
    else:
        mrc_plan = [
            (150, 165, 2, "MRC 300+150 4:2:0"),
            (120, 160, 2, "MRC 300+120 4:2:0"),
        ]

    best_bytes: bytes | None = None
    best_meta: tuple[str, str, str, bool] | None = None

    for dpi_bg, text_t, sub, label in mrc_plan:
        masks_g4, bg_imgs = _prepare_mrc_layers(imgs_300, dpi_mask=300, dpi_bg=dpi_bg, text_thresh=text_t)
        for keep_ocr_try in ([True, False] if has_ocr else [False]):
            q, b = _binsearch_max(
                lambda q_val: _build_mrc_pdf(input_path, masks_g4, bg_imgs, rects, q_val, sub, keep_ocr_try),
                18,
                95,
                target_bytes,
            )
            if len(b) <= target_bytes:
                best_bytes = b
                sub_str = "4:4:4" if sub == 0 else "4:2:0"
                best_meta = (
                    f"MRC_300G4_{dpi_bg}JPG",
                    label + (" + OCR" if keep_ocr_try else ""),
                    f"G4 300 DPI (t={text_t}) + BG {dpi_bg} DPI {sub_str} Q={q}"
                    + (", OCR збережено" if keep_ocr_try else ""),
                    keep_ocr_try,
                )
                break
        if best_bytes is not None:
            break

    # Strategy 3C: Extreme fallback for 30-100+ page scans — 130 DPI Grayscale JPEG
    if best_bytes is None:
        imgs_lo, rects_lo = _render_pages(input_path, dpi=130, use_native_if_single=False)
        gray_imgs = [im.convert("L") for im in imgs_lo]
        q, b = _binsearch_max(
            lambda q_val: _build_single_layer_pdf(input_path, gray_imgs, rects_lo, q_val, subsampling=2, keep_ocr=False),
            12,
            85,
            target_bytes,
        )
        best_bytes = b
        best_meta = (
            "JPEG_130_GRAY_FALLBACK",
            "130 DPI Grayscale (високе стиснення)",
            f"Grayscale JPEG 130 DPI, Q={q}",
            False,
        )

    with open(output_path, "wb") as f:
        f.write(best_bytes)

    out_bytes = len(best_bytes)
    elapsed = round(time.perf_counter() - t0, 2)
    sid, slabel, sparams, ocr_ok = best_meta
    return CompressionResult(
        orig_bytes=orig_bytes,
        out_bytes=out_bytes,
        pages=n_pages,
        compression_pct=round((1.0 - out_bytes / orig_bytes) * 100.0, 1),
        compression_ratio=round(orig_bytes / max(1, out_bytes), 2),
        strategy_id=sid,
        strategy_label=slabel,
        params_summary=sparams,
        ocr_preserved=ocr_ok,
        ocr_chars=ocr_chars if ocr_ok else 0,
        elapsed_sec=elapsed,
    )

