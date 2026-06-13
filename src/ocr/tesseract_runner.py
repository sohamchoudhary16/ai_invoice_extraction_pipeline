"""
src/ocr/tesseract_runner.py
Runs Tesseract on a single PIL Image.
Returns structured OCR output with per-word bounding boxes + confidence.
"""

import pytesseract
from PIL import Image
from dataclasses import dataclass
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class OcrWord:
    text: str
    confidence: int          # 0–100; -1 = non-text block
    left: int
    top: int
    width: int
    height: int
    block_num: int
    line_num: int
    word_num: int


@dataclass
class OcrPageResult:
    page_number: int
    words: list[OcrWord]
    avg_confidence: float
    full_text: str           # words in reading order, space-joined
    psm_used: int
    lang_used: str


def run_ocr(
    image: Image.Image,
    page_number: int,
    psm: int = 6,
    lang: str = "deu+eng",
) -> OcrPageResult:
    """
    Run Tesseract and return a structured OcrPageResult.

    psm modes relevant for invoices:
      3  = fully automatic (good default)
      6  = single uniform block (good for clean single-column invoices)
      11 = sparse text (good for noisy multi-column)
    """
    config = f"--psm {psm} --oem 3"
    data = pytesseract.image_to_data(
        image,
        lang=lang,
        config=config,
        output_type=pytesseract.Output.DICT,
    )

    words: list[OcrWord] = []
    valid_confs: list[int] = []

    for i, raw_word in enumerate(data["text"]):
        word = raw_word.strip()
        conf = int(data["conf"][i])

        if not word or conf < 0:
            continue

        words.append(OcrWord(
            text=word,
            confidence=conf,
            left=int(data["left"][i]),
            top=int(data["top"][i]),
            width=int(data["width"][i]),
            height=int(data["height"][i]),
            block_num=int(data["block_num"][i]),
            line_num=int(data["line_num"][i]),
            word_num=int(data["word_num"][i]),
        ))
        valid_confs.append(conf)

    avg_conf = sum(valid_confs) / len(valid_confs) if valid_confs else 0.0
    full_text = " ".join(w.text for w in words)

    log.info("ocr_complete",
             page=page_number, words=len(words),
             avg_conf=round(avg_conf, 1), psm=psm, lang=lang)

    return OcrPageResult(
        page_number=page_number,
        words=words,
        avg_confidence=round(avg_conf, 2),
        full_text=full_text,
        psm_used=psm,
        lang_used=lang,
    )
