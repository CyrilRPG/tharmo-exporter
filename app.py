# app.py — Streamlit + Playwright
# Upload HTML → 1 PDF par fichier (aucune fusion)

import re
import streamlit as st
from playwright.sync_api import sync_playwright, Error as PwError

APP_TITLE = "HTML → PDF (Playwright)"
st.set_page_config(page_title=APP_TITLE, layout="centered")
st.title(APP_TITLE)
st.caption("Uploader un ou plusieurs fichiers HTML. Un PDF est généré pour chaque fichier.")

# ========= Interface =========
with st.form("params"):
    html_files = st.file_uploader(
        "Uploader un ou plusieurs fichiers HTML",
        type=["html", "htm"],
        accept_multiple_files=True,
    )
    submitted = st.form_submit_button("Générer les PDF")

# ========= PDF =========
def html_to_pdf_bytes(play, html: str) -> bytes:
    browser = play.chromium.launch(headless=True)
    try:
        context = browser.new_context()
        page = context.new_page()
        page.set_content(html, wait_until="domcontentloaded")
        page.emulate_media(media="print")
        pdf = page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
        )
        context.close()
        return pdf
    finally:
        browser.close()

# ========= Exécution =========
if submitted:

    if not html_files:
        st.error("Aucun fichier HTML fourni.")
        st.stop()

    with st.spinner("Génération des PDF…"):
        try:
            with sync_playwright() as play:
                for f in html_files:
                    raw = f.read()

                    try:
                        html = raw.decode("utf-8")
                    except Exception:
                        html = raw.decode("latin-1", errors="replace")

                    pdf_bytes = html_to_pdf_bytes(play, html)

                    base_name = re.sub(r"\.(html|htm)$", "", f.name, flags=re.I)
                    pdf_name = f"{base_name}.pdf"

                    st.success(f"PDF généré : {pdf_name}")
                    st.download_button(
                        label=f"Télécharger {pdf_name}",
                        data=pdf_bytes,
                        file_name=pdf_name,
                        mime="application/pdf",
                        key=f"dl_{f.name}",
                    )

        except PwError as e:
            st.error(f"Erreur Playwright : {e}")

        except Exception as e:
            st.error(f"Erreur inattendue : {e}")

    st.stop()
