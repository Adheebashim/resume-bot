"""
PDF Renderer Module - LibreOffice docx->PDF conversion + ReportLab custom resume layout.
"""

import os
import io
import re
import shutil
import tempfile
import subprocess
from pathlib import Path

from pypdf import PdfReader
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable

from config.settings import OWNER_NAME, REFERENCE_LINKS


def docx_to_pdf(docx_bytes: bytes) -> bytes:
    """Convert a tailored Word file using LibreOffice, installed in the Render build."""
    executable = (
        shutil.which("libreoffice") or shutil.which("soffice")
        or next((p for p in ("/usr/bin/soffice", "/usr/bin/libreoffice",
                             "/usr/lib/libreoffice/program/soffice",
                             "/opt/libreoffice/program/soffice") if os.path.exists(p)), None)
    )
    if not executable:
        raise RuntimeError(
            "LibreOffice is not installed in this deployment. This service must be deployed "
            "from the Dockerfile (Render > Settings > Language = Docker); the Python runtime "
            "cannot install it."
        )
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        docx_path = temp / "Adheeb_Tailored_Resume.docx"
        docx_path.write_bytes(docx_bytes)
        result = subprocess.run(
            [executable, "-env:UserInstallation=file://" + str(temp / "lo-profile"),
             "--headless", "--convert-to", "pdf", "--outdir", str(temp), str(docx_path)],
            capture_output=True, text=True, timeout=90, check=False,
        )
        pdf_path = temp / "Adheeb_Tailored_Resume.pdf"
        if result.returncode != 0 or not pdf_path.exists():
            detail = (result.stderr or result.stdout or "No conversion details").strip()
            raise RuntimeError("Word-to-PDF conversion failed: " + detail[:500])
        pdf = pdf_path.read_bytes()
        reader = PdfReader(io.BytesIO(pdf))
        if not reader.pages or len(reader.pages) > 3:
            raise RuntimeError(f"Generated resume has {len(reader.pages)} pages. Please shorten content.")
        if len("\n".join(p.extract_text() or "" for p in reader.pages).strip()) < 250:
            raise RuntimeError("Generated PDF text validation failed. Please retry.")
        return pdf


def make_pdf(text: str) -> bytes:
    """Create a compact, professional resume PDF matching the reference layout."""
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=43, rightMargin=43,
        topMargin=31, bottomMargin=31, title=f"{OWNER_NAME} - Tailored Resume",
        author=OWNER_NAME,
    )

    name_style = ParagraphStyle(
        "ResumeName", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=22, leading=24, alignment=TA_CENTER,
        textColor=colors.HexColor("#17202A"), spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "ResumeSubtitle", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=10, leading=12, alignment=TA_CENTER, spaceAfter=3,
    )
    contact_style = ParagraphStyle(
        "ResumeContact", parent=styles["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=10, alignment=TA_CENTER, spaceAfter=2,
    )
    body_style = ParagraphStyle(
        "ResumeBody", parent=styles["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=10.4, alignment=TA_LEFT, spaceAfter=1.8,
        textColor=colors.HexColor("#202124"),
    )
    bullet_style = ParagraphStyle(
        "ResumeBullet", parent=body_style, leftIndent=11, firstLineIndent=-8,
        spaceAfter=2,
    )
    section_style = ParagraphStyle(
        "ResumeSection", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=10, leading=11.5, spaceBefore=7, spaceAfter=3,
        textColor=colors.HexColor("#17202A"), keepWithNext=True,
    )
    role_style = ParagraphStyle(
        "ResumeRole", parent=body_style, fontName="Helvetica-Bold",
        fontSize=8.5, leading=10.5, spaceBefore=3, spaceAfter=2, keepWithNext=True,
    )
    small_style = ParagraphStyle(
        "ResumeSmall", parent=body_style, fontSize=7.8, leading=9.5,
    )

    section_names = {
        "SUMMARY", "PROFILE", "PROFESSIONAL SUMMARY", "EXPERIENCE",
        "WORK EXPERIENCE", "PROJECTS", "SELECTED PROJECTS",
        "INDEPENDENT ANALYTICAL SOLUTIONS & AI PROJECTS", "SKILLS",
        "TECHNICAL SKILLS", "EDUCATION", "CERTIFICATIONS", "ACHIEVEMENTS",
        "PUBLICATIONS",
    }
    story = []
    lines = [line.strip() for line in text.replace("\r", "").splitlines() if line.strip()]
    first = lines[0] if lines else ""
    has_name_header = bool(re.search(r"adheeb|^[A-Z][A-Za-z]+\s+[A-Z][A-Za-z]+$", first, re.I))

    if has_name_header:
        story.append(Paragraph(escape(first), name_style))
        lines = lines[1:]
        header_count = 0
        while lines and header_count < 3:
            candidate = lines[0]
            if candidate.upper().rstrip(":") in section_names or candidate.startswith(("-", "•", "–")):
                break
            story.append(Paragraph(escape(candidate), subtitle_style if header_count == 0 else contact_style))
            lines = lines[1:]
            header_count += 1
        story.append(Spacer(1, 4))
    else:
        story.append(Paragraph(OWNER_NAME.upper(), name_style))
        story.append(Paragraph("Data Engineer · AI Engineer · Data Analyst", subtitle_style))
        story.append(Paragraph(
            'Nilambur, Kerala (Open to Relocate) · +91 8281759103 / +91 7306134684 · '
            'adheebashim1010@gmail.com', contact_style))
        links_html = ' · '.join(
            f'<link href="{url}" color="#1A73E8"><u>{label}</u></link>'
            for label, url in REFERENCE_LINKS.items()
        )
        story.append(Paragraph(links_html, contact_style))
        story.append(Spacer(1, 4))

    for raw in lines:
        line = raw.strip()
        if re.fullmatch(r"(?:LinkedIn|GitHub|Portfolio)(?:\s*[·|•/]\s*(?:LinkedIn|GitHub|Portfolio))*", line, re.I):
            links_html = ' · '.join(
                f'<link href="{url}" color="#1A73E8"><u>{label}</u></link>'
                for label, url in REFERENCE_LINKS.items()
            )
            story.append(Paragraph(links_html, contact_style))
            continue
        normalized = re.sub(r"[:\s]+$", "", line).upper()
        if normalized in section_names or (len(line) < 42 and line.isupper() and not line.startswith(("-", "•"))):
            story.append(Spacer(1, 2))
            story.append(Paragraph(escape(normalized), section_style))
            story.append(HRFlowable(width="100%", thickness=0.55, color=colors.HexColor("#AAB2BD"), spaceBefore=0, spaceAfter=3))
        elif line.startswith(("-", "•", "–", "*")):
            story.append(Paragraph("– " + escape(line[1:].strip()), bullet_style))
        elif len(line) < 105 and (" — " in line or " – " in line or " | " in line or (line.endswith(":") and len(line) < 65)):
            story.append(Paragraph(escape(line), role_style))
        elif re.match(r"^(Technologies|Tools|Skills|Languages|Analytics|AI &|Generative AI|Operational Delivery)\s*:", line, re.I):
            label, value = line.split(":", 1)
            story.append(Paragraph(f"<b>{escape(label)}:</b>{escape(value)}", small_style))
        else:
            story.append(Paragraph(escape(line), body_style))

    doc.build(story)
    return buffer.getvalue()
