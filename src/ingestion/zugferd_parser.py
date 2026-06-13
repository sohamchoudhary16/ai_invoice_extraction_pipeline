"""
src/utils.py  —  PDF byte reader + XML helpers (shared base class)

Lives in src/ so every submodule can import it as:
    from src.utils import utils
"""
import io
from pypdf import PdfReader
from lxml  import etree


class utils:

    # ZUGFeRD 1.0 namespace map
    NS = {
        "rsm": "urn:ferd:CrossIndustryDocument:invoice:1p0",
        "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:12",
        "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:15",
    }

    # ── PDF helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def read_pdf_bytes(pdf_path: str) -> bytes:
        with open(pdf_path, "rb") as f:
            return f.read()

    @staticmethod
    def extract_embedded_xml(pdf_bytes: bytes) -> dict[str, bytes]:
        """Return {filename: xml_bytes} for every embedded XML in the PDF."""
        reader   = PdfReader(io.BytesIO(pdf_bytes))
        root     = reader.trailer.get("/Root", {})
        embedded = root.get("/Names", {}).get("/EmbeddedFiles")
        if not embedded:
            return {}
        items, out = embedded.get("/Names", []), {}
        for i in range(0, len(items), 2):
            fname   = str(items[i])
            ef      = items[i + 1].get("/EF", {})
            fstream = ef.get("/F")
            if fstream and fname.lower().endswith(".xml"):
                out[fname] = fstream.get_data()
        return out

    @staticmethod
    def get_zugferd_xml(pdf_path: str) -> bytes | None:
        """Read a PDF and return its first embedded XML payload, or None."""
        xml_map = utils.extract_embedded_xml(utils.read_pdf_bytes(pdf_path))
        return next(iter(xml_map.values()), None) if xml_map else None

    # ── XML helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def xp(el, path: str) -> str | None:
        """Safe xpath — returns stripped first-text-result or None."""
        if el is None:
            return None
        result = el.xpath(path, namespaces=utils.NS)
        return result[0].strip() if result else None

    @staticmethod
    def get_node(root, xpath: str):
        """Return first matching node or None (never raises IndexError)."""
        result = root.xpath(xpath, namespaces=utils.NS)
        return result[0] if result else None
