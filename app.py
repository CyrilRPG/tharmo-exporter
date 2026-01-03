# app.py — Streamlit + Playwright (export Sujet + Corrigé depuis tHarmo)
#
# Version sans BeautifulSoup, guillemets normalisés (pas d'erreur de chaîne).
#
# MODIF UNIQUE DEMANDÉE :
# - Upload multiple de fichiers HTML
# - 1 PDF généré par fichier HTML (aucune fusion)
# - Le reste du code est inchangé

import re
from html import unescape as html_unescape, escape as html_escape

import streamlit as st
from playwright.sync_api import sync_playwright, Error as PwError

APP_TITLE = "tHarmo → PDF (Sujet + Corrigé)"
st.set_page_config(page_title=APP_TITLE, layout="centered")
st.title(APP_TITLE)
st.caption("Entrez vos identifiants tHarmo + l’ID d’épreuve. L’appli génère 2 PDF à télécharger.")

# ========= Interface utilisateur =========
with st.form("params"):
    base = st.text_input("Base tHarmo", "https://pass.tharmo.tutotours.fr").strip().rstrip("/")
    username = st.text_input("Email / Identifiant tHarmo", value="", autocomplete="username")
    password = st.text_input("Mot de passe tHarmo", value="", type="password", autocomplete="current-password")

    ids_text = st.text_area(
        "ID(s) d’épreuve (un par ligne ou collez l’URL)",
        placeholder="1914339\nhttps://pass.tharmo.tutotours.fr/banque/qc/entrainement/qcmparqcm/idEpreuve=1914315",
        height=100,
    )

    html_files = st.file_uploader(
        "Uploader un ou plusieurs fichiers HTML (1 PDF sera généré par fichier)",
        type=["html", "htm"],
        accept_multiple_files=True,
    )

    submitted = st.form_submit_button("Exporter (Sujet + Corrigé)")

# ========= Utilitaires =========
def parse_ids(txt: str):
    out = []
    for line in (txt or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            out.append(line)
        else:
            m = re.search(r"(?:idEpreuve|idepreuve)\s*=\s*(\d+)", line, re.I)
            if m:
                out.append(m.group(1))
    dedup, seen = [], set()
    for x in out:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup


def dismiss_banners(page):
    try:
        selectors = [
            "button:has-text(\"Accepter\")",
            "button:has-text(\"J'accepte\")",
            "button:has-text(\"J’accepte\")",
            "button:has-text(\"OK\")",
            "button:has-text(\"D'accord\")",
            "button:has-text(\"D’accord\")",
            "button:has-text(\"Compris\")",
            "button:has-text(\"Fermer\")",
            "[aria-label=\"Fermer\"]",
            "#didomi-notice-agree-button",
            "button.cookie-accept",
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            if loc.count() > 0:
                try:
                    loc.click()
                except Exception:
                    pass
    except Exception:
        pass


def try_login(page, base, username, password) -> bool:
    page.goto(base + "/banque/qc/entrainement/", wait_until="domcontentloaded")
    dismiss_banners(page)
    if page.locator("input[type=\"password\"]").count() == 0:
        return True
    try:
        page.locator("input[type=\"email\"]").first.fill(username)
        page.locator("input[type=\"password\"]").first.fill(password)
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded")
        dismiss_banners(page)
        return True
    except Exception:
        return False


def html_to_pdf_bytes(play, html: str) -> bytes:
    browser = play.chromium.launch(headless=True)
    try:
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        p = ctx.new_page()
        p.set_content(html, wait_until="domcontentloaded")
        p.emulate_media(media="print")
        pdf_bytes = p.pdf(
            format="A4",
            print_background=True,
            margin={"top": "10mm", "right": "10mm", "bottom": "12mm", "left": "10mm"},
        )
        ctx.close()
        return pdf_bytes
    finally:
        browser.close()


# ========= Exécution =========
if submitted:

    # ===== MODE HTML → 1 PDF PAR FICHIER =====
    if html_files:
        with st.spinner("Génération des PDF depuis les HTML…"):
            try:
                with sync_playwright() as play:
                    for f in html_files:
                        raw = f.read()
                        try:
                            html_in = raw.decode("utf-8")
                        except Exception:
                            html_in = raw.decode("latin-1", errors="replace")

                        pdf_bytes = html_to_pdf_bytes(play, html_in)
                        base_name = re.sub(r"\.(html|htm)$", "", f.name, flags=re.I)
                        pdf_name = f"{base_name}.pdf"

                        st.success(f"PDF généré : {pdf_name}")
                        st.download_button(
                            f"Télécharger {pdf_name}",
                            data=pdf_bytes,
                            file_name=pdf_name,
                            mime="application/pdf",
                            key=f"dl_{f.name}",
                        )
            except Exception as e:
                st.error(f"Erreur lors de la génération des PDF : {e}")

        st.stop()

    # ===== MODE tHarmo CLASSIQUE =====
    ids = parse_ids(ids_text)
    if not username or not password or not ids:
        st.error("Renseigne identifiants + au moins un ID d’épreuve.")
        st.stop()

    st.info("Mode tHarmo actif (HTML non fourni).")
