"""OCR locale dei PDF scansionati con Apple Vision (solo macOS, nessun servizio esterno)."""
from __future__ import annotations
import sys

MAX_PAGES = 40
LANGS = ["it-IT", "en-US"]


def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Quartz, Vision  # noqa: F401  (pyobjc-framework-Quartz / -Vision)
        return True
    except Exception:
        return False


def ocr_pdf(path, max_pages: int = MAX_PAGES) -> list[tuple[int, str]]:
    import objc, Quartz, Vision
    from Foundation import NSURL, NSMakeSize

    pages = []
    with objc.autorelease_pool():
        doc = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(str(path)))
        if doc is None:
            raise ValueError("PDF non apribile per l'OCR")
        for i in range(min(doc.pageCount(), max_pages)):
            with objc.autorelease_pool():
                page = doc.pageAtIndex_(i)
                box = page.boundsForBox_(Quartz.kPDFDisplayBoxMediaBox)
                scale = 2.0 if max(box.size.width, box.size.height) < 1200 else 1.5
                img = page.thumbnailOfSize_forBox_(NSMakeSize(box.size.width * scale, box.size.height * scale),
                                                   Quartz.kPDFDisplayBoxMediaBox)
                cg = img.CGImageForProposedRect_context_hints_(None, None, None)[0]
                req = Vision.VNRecognizeTextRequest.alloc().init()
                req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
                req.setRecognitionLanguages_(LANGS)
                req.setUsesLanguageCorrection_(True)
                handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
                ok, err = handler.performRequests_error_([req], None)
                if not ok:
                    continue
                lines = [o.topCandidates_(1)[0].string() for o in (req.results() or []) if o.topCandidates_(1)]
                if lines:
                    pages.append((i + 1, "\n".join(lines)))
    return pages
