# app.py — Streamlit + Playwright (export Sujet + Corrigé depuis tHarmo)
#
# Version sans BeautifulSoup, guillemets normalisés (pas d'erreur de chaîne).
#
# MODIF UNIQUE DEMANDÉE :
# - Ajout d’un upload multiple de fichiers HTML.
# - Si vous uploadez plusieurs HTML, l’app génère un PDF (1 par HTML) à télécharger.
# - Le reste du code est inchangé (export tHarmo Sujet + Corrigé via identifiants + ID d’épreuve).

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

    # ====== MODIF : upload multiple HTML ======
    html_files = st.file_uploader(
        "Uploader un ou plusieurs fichiers HTML (1 PDF sera généré par fichier)",
        type=["html", "htm"],
        accept_multiple_files=True,
    )
    # =========================================

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


def html2txt(html: str) -> str:
    """Simplifie du HTML en texte."""
    if not html:
        return ""
    html = re.sub(r"(?is)<\s*sup\s*>(.*?)</\s*sup\s*>", lambda m: "^" + m.group(1), html)
    tmp = re.sub(r"(?is)<[^>]+>", "", html)
    tmp = html_unescape(tmp)
    return re.sub(r"\s+", " ", tmp).strip()


def dismiss_banners(page):
    """Ferme les modales/cookies éventuelles (chaînes protégées)."""
    try:
        selectors = [
            "button:has-text(\"Accepter\")",
            "button:has-text(\"J'accepte\")",   # apostrophe droite
            "button:has-text(\"J’accepte\")",   # apostrophe courbe
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
        page.evaluate(
            """() => {
                for (const s of ['.cookie', '.modal', '#cookie', '.overlay', '.consent']) {
                    document.querySelectorAll(s).forEach(n => {
                        const z = parseInt(getComputedStyle(n).zIndex||'0',10);
                        if (z >= 1000) n.style.display = 'none';
                    });
                }
            }"""
        )
    except Exception:
        pass


def try_login(page, base, username, password) -> bool:
    """Se connecte si nécessaire."""
    page.goto(base + "/banque/qc/entrainement/", wait_until="domcontentloaded")
    dismiss_banners(page)
    if page.locator("input[type=\"password\"]").count() == 0:
        return True
    try:
        email_sel = "input[type=\"email\"], input[name*=\"mail\" i], input[name*=\"user\" i], input[name*=\"login\" i]"
        pwd_sel   = "input[type=\"password\"]"
        btn_sel   = "button:has-text(\"Connexion\"), input[type=\"submit\"], button[type=\"submit\"]"
        page.locator(email_sel).first.fill(username)
        page.locator(pwd_sel).first.fill(password)
        if page.locator(btn_sel).count() > 0:
            page.locator(btn_sel).first.click()
        else:
            page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded")
        dismiss_banners(page)
        return True
    except Exception:
        return False


def start_correction(page, base, eid) -> bool:
    """Ouvre l’épreuve et passe en mode correction."""
    page.goto(f"{base}/banque/qc/entrainement/qcmparqcm/idEpreuve={eid}", wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    dismiss_banners(page)
    if page.locator("#correction").count() > 0:
        page.locator("#correction").first.click()
        page.wait_for_load_state("domcontentloaded")
        dismiss_banners(page)
        return True
    page.goto(f"{base}/banque/qc/entrainement/correction/commencer/fin=0/id={eid}", wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    dismiss_banners(page)
    return True


def extract_current(page):
    """
    Extrait la question courante et ses items (Sujet, Correction, Réponse) depuis la page de correction.
    Retourne {"title": str, "items": [...] } ou None.
    """
    try:
        card = page.locator(".card.card-content").first
        if card.count() == 0:
            return None
        title_el = card.locator("div.card-title").first
        title = (title_el.text_content() or "").strip() if title_el.count() else ""
        items = []

        for span in card.locator("span.card-title").all():
            text = (span.text_content() or "").strip()
            if not re.match(r"^Item\s+[A-E]", text, flags=re.I):
                continue
            letter = text.split()[1].strip().upper()
            row = span.locator("xpath=following-sibling::*[contains(@class,'row')][1]")
            if row.count() == 0:
                continue
            cols = row.locator(":scope > div").all()
            if not cols:
                continue

            sujet = corr = rep = ""
            is_true = is_false = False

            # Sujet
            col0 = cols[0]
            p0 = col0.locator("p").first
            text_sujet = (p0.inner_text() if p0.count() else col0.inner_text()).strip()
            sujet = re.sub(r"^Sujet\s*:?\s*", "", text_sujet, flags=re.I)

            # Correction + verdict
            if len(cols) >= 2:
                col1 = cols[1]
                p1 = col1.locator("p").first
                text_corr = (p1.inner_text() if p1.count() else col1.inner_text()).strip()
                corr = re.sub(r"^Correction\s*:?\s*", "", text_corr, flags=re.I)
                is_true  = col1.locator(".green-text").count() > 0
                is_false = col1.locator(".red-text").count()  > 0

            # Réponse
            if len(cols) >= 3:
                col2 = cols[2]
                p2 = col2.locator("p").first
                text_rep = (p2.inner_text() if p2.count() else col2.inner_text()).strip()
                rep = re.sub(r"^Votre r[ée]ponse\s*:?\s*", "", text_rep, flags=re.I)

            items.append({
                "letter": letter,
                "sujet": sujet,
                "correction": corr,
                "reponse": rep,
                "isTrue": is_true,
                "isFalse": is_false,
            })

        if items:
            return {"title": title, "items": items}
    except Exception:
        pass
    return None


def render_pdf_html(eid: str, captured: list, mode: str) -> str:
    header = f"<h1>{'Sujet' if mode=='sujet' else 'Corrigé'} – QCM tHarmo – Épreuve {html_escape(eid)}</h1>"
    parts = [
        """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>PDF</title>
<style>
@page{size:A4;margin:16mm}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;line-height:1.35}
h1{font-size:18pt;margin:0 0 8mm}
h2{font-size:12pt;margin:8mm 0 4mm}
.qcm{break-inside:avoid;margin-bottom:10mm;padding-bottom:5mm;border-bottom:1px solid #ddd}
.lines{margin-left:2mm}
.line{margin:2mm 0}
.muted{color:#666;font-size:10pt}
.corr{margin-top:2mm;padding:3mm;background:#f6f8fa;border-left:3px solid #c5e1a5}
.badge{font-weight:700}
</style></head><body>""",
        header,
        f"<p class='muted'>Questions exportées : {len(captured)}</p>",
    ]

    def esc(s): return html_escape((s or ""))

    for i, q in enumerate(captured, start=1):
        parts.append(f"<section class='qcm'><h2>{i}. {esc(q['title'])}</h2>")
        parts.append("<div class='lines'>")

        for it in q["items"]:
            if mode == "sujet":
                parts.append(f"<div class='line'><strong>{esc(it['letter'])}</strong> — {esc(it['sujet'])}</div>")
            else:
                vf = "✔ Vrai" if it["isTrue"] else ("✖ Faux" if it["isFalse"] else "•")
                corr = f" – {esc(it['correction'])}" if it["correction"] else ""
                rep  = f" (Votre réponse : {esc(it['reponse'])})" if it["reponse"] else ""
                parts.append(
                    f"<div class='line'><strong>{esc(it['letter'])}</strong> — {esc(it['sujet'])}"
                    f"<div class='corr'><span class='badge'>{vf}</span>{corr}{rep}</div></div>"
                )

        parts.append("</div></section>")

    parts.append("</body></html>")
    return "".join(parts)


def html_to_pdf_bytes(play, html: str) -> bytes:
    """Génère un PDF à partir du HTML via un contexte Playwright séparé."""
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
    # ====== MODIF : si upload HTML(s), générer 1 PDF par fichier (et ne pas exiger identifiants/IDs) ======
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

                        # 1 PDF par HTML (tel quel)
                        try:
                            pdf_bytes = html_to_pdf_bytes(play, html_in)
                            base_name = re.sub(r"\.(html|htm)$", "", (f.name or "document"), flags=re.I)
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
                            st.error(f"Erreur PDF pour {f.name} : {e}")

            except PwError as e:
                st.error(f"Erreur Playwright : {e}")
            except Exception as e:
                st.error(f"Erreur inattendue : {e}")

        st.stop()
    # ================================================================================================

    ids = parse_ids(ids_text)
    if not username or not password or not ids:
        st.error("Renseigne identifiants + au moins un ID d’épreuve.")
        st.stop()

    with st.spinner("Lancement du navigateur…"):
        try:
            with sync_playwright() as play:
                browser = play.chromium.launch(headless=True)
                try:
                    context = browser.new_context(viewport={"width": 1280, "height": 900})
                    page = context.new_page()
                    page.set_default_timeout(0)
                    context.set_default_timeout(0)

                    # Connexion
                    if not try_login(page, base, username, password):
                        st.error("Échec de connexion. Vérifie identifiants.")
                        st.stop()

                    for eid in ids:
                        st.write(f"### Épreuve {eid}")
                        if not start_correction(page, base, eid):
                            st.error("Impossible d’atteindre la correction.")
                            continue

                        captured = []
                        seen = set()

                        while True:
                            data = extract_current(page)
                            if data and data.get("items"):
                                fp = data["title"] + (data["items"][0]["letter"] if data["items"] else "")
                                if fp not in seen:
                                    seen.add(fp)
                                    captured.append({
                                        "title": (data.get("title") or "").strip(),
                                        "items": data["items"],
                                    })
                                    st.write(f"✓ Capturé {len(captured)} : {captured[-1]['title'] or '(sans titre)'}")

                            # Suivante ?
                            if page.locator("#nextQuestionButton").count() > 0:
                                try:
                                    page.locator("#nextQuestionButton").first.click()
                                    page.wait_for_load_state("domcontentloaded")
                                except Exception:
                                    break
                            else:
                                break
                            dismiss_banners(page)

                            # Revenir en mode correction
                            if page.locator("#correction").count() > 0:
                                try:
                                    page.locator("#correction").first.click()
                                    page.wait_for_load_state("domcontentloaded")
                                except Exception:
                                    pass
                            dismiss_banners(page)

                        if not captured:
                            st.warning("Aucune question capturée.")
                            continue

                        # PDF Sujet + Corrigé
                        html_sujet = render_pdf_html(eid, captured, "sujet")
                        html_corr  = render_pdf_html(eid, captured, "corrige")
                        sujet_name = f"qcm_tharmo_{eid}_sujet.pdf"
                        corr_name  = f"qcm_tharmo_{eid}_corrige.pdf"

                        try:
                            sujet_bytes = html_to_pdf_bytes(play, html_sujet)
                            corr_bytes  = html_to_pdf_bytes(play, html_corr)
                            st.success("PDF générés.")
                            st.download_button(
                                "⬇️ Télécharger Sujet",
                                data=sujet_bytes,
                                file_name=sujet_name,
                                mime="application/pdf",
                            )
                            st.download_button(
                                "⬇️ Télécharger Corrigé",
                                data=corr_bytes,
                                file_name=corr_name,
                                mime="application/pdf",
                            )
                        except Exception as e:
                            st.error(f"Erreur PDF : {e}")

                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
                    browser.close()
        except PwError as e:
            st.error(f"Erreur Playwright : {e}")
        except Exception as e:
            st.error(f"Erreur inattendue : {e}")

st.divider()
with st.expander("Notes & Conseils"):
    st.markdown(
        """
- **Identifiants** : ils sont utilisés uniquement pour ouvrir une session le temps de l’export.
- **Respecte les CGU** de tHarmo / Tutorat (usage personnel).
- Si une épreuve ne s’exporte pas, fournis l’**ID** exact (ou l’URL) et réessaie.
"""
    )
