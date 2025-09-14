# app.py — Streamlit + Playwright — Export Sujet + Corrigé depuis tHarmo
# Fonctionne avec analyse HTML via BeautifulSoup pour extraire les questions.

import re
import time
from html import unescape as html_unescape, escape as html_escape

import streamlit as st
from playwright.sync_api import sync_playwright, Error as PwError
from bs4 import BeautifulSoup  # pour parser le HTML

APP_TITLE = "tHarmo → PDF (Sujet + Corrigé)"
st.set_page_config(page_title=APP_TITLE, layout="centered")
st.title(APP_TITLE)
st.caption(
    "Entrez vos identifiants tHarmo + l’ID d’épreuve. L’appli génère 2 PDF à télécharger."
)

# =============== UI ===============
with st.form("params"):
    base = st.text_input("Base tHarmo", "https://pass.tharmo.tutotours.fr").strip().rstrip("/")
    username = st.text_input(
        "Email / Identifiant tHarmo", value="", autocomplete="username"
    )
    password = st.text_input(
        "Mot de passe tHarmo", value="", type="password", autocomplete="current-password"
    )
    ids_text = st.text_area(
        "ID(s) d’épreuve (un par ligne ou collez l’URL)",
        placeholder="1914339\nhttps://pass.tharmo.tutotours.fr/banque/qc/entrainement/qcmparqcm/idEpreuve=1914315",
        height=100,
    )
    submitted = st.form_submit_button("Exporter (Sujet + Corrigé)")

# =============== Utils ===============
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
    """Convertit un fragment HTML en texte simplifié."""
    if not html:
        return ""
    html = re.sub(r"(?is)<\s*sup\s*>(.*?)</\s*sup\s*>", lambda m: "^" + m.group(1), html)
    tmp = re.sub(r"(?is)<[^>]+>", "", html)
    tmp = html_unescape(tmp)
    return re.sub(r"\s+", " ", tmp).strip()

def looks_like_login(page) -> bool:
    try:
        if re.search(r"(login|connexion|auth)", page.url, re.I):
            return True
        if page.locator('input[type="password"]').count() > 0:
            return True
        if page.locator('button:has-text("Connexion"), input[type="submit"]').count() > 0:
            return True
    except Exception:
        pass
    return False

def dismiss_banners(page):
    """Ferme les bannières, cookies, modales susceptibles de bloquer les actions."""
    try:
        for sel in [
            'button:has-text("Accepter")',
            'button:has-text("J\'accepte")',
            'button:has-text("OK")',
            'button:has-text("D\'accord")',
            'button:has-text("Compris")',
            'button:has-text("Fermer")',
            '[aria-label="Fermer"]',
            '#didomi-notice-agree-button',
            'button.cookie-accept',
        ]:
            if page.locator(sel).first.count() > 0:
                try:
                    page.locator(sel).first.click()
                except Exception:
                    pass
        # masque les overlays avec un z-index élevé
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
    """Navigue vers une page protégée et se connecte si nécessaire."""
    page.goto(base + "/banque/qc/entrainement/", wait_until="domcontentloaded")
    dismiss_banners(page)
    if not looks_like_login(page):
        return True
    try:
        email_sel = (
            'input[type="email"], input[name*="mail" i], input[name*="user" i], input[name*="login" i]'
        )
        pwd_sel = 'input[type="password"]'
        btn_sel = 'button:has-text("Connexion"), input[type="submit"], button[type="submit"]'
        page.locator(email_sel).first.fill(username)
        page.locator(pwd_sel).first.fill(password)
        if page.locator(btn_sel).count() > 0:
            page.locator(btn_sel).first.click()
        else:
            page.keyboard.press("Enter")
        page.wait_for_url(lambda u: re.search(r"(login|connexion|auth)", u, re.I) is None)
        page.wait_for_load_state("domcontentloaded")
        dismiss_banners(page)
        return True
    except Exception:
        return False

def start_correction(page, base, epreuve_id) -> bool:
    """Accède à l’épreuve et lance la correction."""
    url_qcm = f"{base}/banque/qc/entrainement/qcmparqcm/idEpreuve={epreuve_id}"
    url_corr = f"{base}/banque/qc/entrainement/correction/commencer/fin=0/id={epreuve_id}"
    page.goto(url_qcm, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    dismiss_banners(page)
    # clic Correction si présent, sinon accéder directement
    if page.locator("#correction").count() > 0:
        page.locator("#correction").first.click()
        page.wait_for_load_state("domcontentloaded")
    else:
        page.goto(url_corr, wait_until="domcontentloaded")
    dismiss_banners(page)
    return ensure_correction_view(page)

def wait_until_correction_ready(page, hard_timeout_ms=60000) -> bool:
    """Attends la présence de #nextQuestionButton et d’au moins un Item A..E."""
    start = time.monotonic()
    while (time.monotonic() - start) * 1000 < hard_timeout_ms:
        dismiss_banners(page)
        has_next = page.locator("#nextQuestionButton").count() > 0
        has_item = (
            page.locator('span.card-title', has_text=re.compile(r"^Item\s+[A-E]$", re.I)).count()
            > 0
        )
        if has_next and has_item:
            return True
        page.wait_for_timeout(200)
    return False

def ensure_correction_view(page, hard_timeout_ms=60000) -> bool:
    """Si on est sur la vue QCM, clique 'Correction' pour basculer en vue Correction."""
    # Si la structure clé est déjà présente, on est en Correction
    if wait_until_correction_ready(page, 2000):
        return True
    # Sinon, tentons de cliquer le bouton Correction depuis la vue QCM
    if page.locator("#correction").count() > 0:
        try:
            page.locator("#correction").first.click()
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            # clic via JS
            try:
                page.evaluate(
                    """() => { const a = document.querySelector('#correction'); if (a) a.click(); }"""
                )
                page.wait_for_load_state("domcontentloaded")
            except Exception:
                pass
    return wait_until_correction_ready(page, hard_timeout_ms)

# =============== Extraction via BeautifulSoup ===============
def extract_bs4(html: str):
    """
    Analyse le HTML d'une page de Correction et extrait question + items.
    Retourne un dict {title: str, enonceHTML: str, items: list} ou None si rien trouvé.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Cherche la carte contenant des Items
    for card in soup.find_all("div", class_="card card-content"):
        # On teste s'il y a un Item X
        if not card.find(string=re.compile(r"^Item\\s+[A-E]", flags=re.I)):
            continue
        title_tag = card.find("div", class_="card-title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        items = []
        for span in card.find_all("span", class_="card-title"):
            txt = span.get_text(strip=True)
            m = re.match(r"^Item\\s+([A-E])", txt, flags=re.I)
            if not m:
                continue
            letter = m.group(1).upper()
            # La <div class="row"> qui suit l'Item
            row = span.find_next_sibling(
                lambda tag: tag.name == "div" and "row" in tag.get("class", [])
            )
            if not row:
                continue
            # Colonnes directes : sujets, correction, réponse
            cols = [
                c
                for c in row.find_all("div", recursive=False)
                if "col" in c.get("class", [])
            ]
            sujet = corr = rep = ""
            is_true = is_false = False
            # Sujet
            if len(cols) >= 1:
                p = cols[0].find("p")
                text = p.get_text(separator=" ", strip=True) if p else cols[0].get_text(strip=True)
                sujet = re.sub(r"^Sujet\\s*:\\s*", "", text, flags=re.I)
            # Correction + verdict
            if len(cols) >= 2:
                p = cols[1].find("p")
                text = p.get_text(separator=" ", strip=True) if p else cols[1].get_text(strip=True)
                corr = re.sub(r"^Correction\\s*:\\s*", "", text, flags=re.I)
                # Cherche des span.bold.green-text / red-text
                bold_span = cols[1].find(
                    lambda tag: tag.name == "span"
                    and "bold" in tag.get("class", [])
                )
                if bold_span:
                    classes = bold_span.get("class", [])
                    if "green-text" in classes:
                        is_true = True
                    if "red-text" in classes:
                        is_false = True
            # Votre réponse
            if len(cols) >= 3:
                p = cols[2].find("p")
                text = p.get_text(separator=" ", strip=True) if p else cols[2].get_text(strip=True)
                rep = re.sub(r"^Votre r[ée]ponse\\s*:\\s*", "", text, flags=re.I)
            items.append(
                {
                    "letter": letter,
                    "sujet": sujet,
                    "correction": corr,
                    "reponse": rep,
                    "isTrue": is_true,
                    "isFalse": is_false,
                }
            )
        if items:
            return {"title": title, "enonceHTML": "", "items": items}
    return None

def extract_current(page):
    """Appelle BeautifulSoup sur le contenu courant de la page Correction."""
    try:
        html = page.content()
    except Exception:
        return None
    return extract_bs4(html)

def fingerprint_any(page) -> str:
    """Empreinte courte : change entre QCM et Correction et à chaque question."""
    try:
        # En Correction : présence de nextQuestionButton
        if page.locator("#nextQuestionButton").count() > 0:
            title = (
                page.locator(".card.card-content .card-title").first.text_content() or ""
            ).strip()
            first_item = (
                page.locator("span.card-title", has_text=re.compile(r"^Item\\s+[A-E]$", re.I))
                .first.text_content()
                or ""
            ).strip()
            return f"CORR|{title}|{first_item}"
        # En QCM
        qcm_title = (
            page.locator(".saut-ligne .bold").first.text_content() or ""
        ).strip()
        first_line = (
            page.locator(".retour-ligne").first.text_content() or ""
        ).strip()
        return f"QCM|{qcm_title}|{first_line}"
    except Exception:
        return str(time.time())

def click_next(page) -> bool:
    """Clique le bouton 'Question suivante' si présent. Retourne False si absent."""
    for sel in [
        "#nextQuestionButton",
        "a#nextQuestion",
        'a:has-text("Question suivante")',
        'button:has-text("Question suivante")',
    ]:
        if page.locator(sel).count() > 0:
            try:
                page.locator(sel).first.click()
                page.wait_for_load_state("domcontentloaded")
                return True
            except Exception:
                pass
    return False

def go_next_and_reenter_correction(page) -> bool:
    """
    Depuis la vue Correction : clic 'Question suivante' (basculer en QCM),
    puis clic 'Correction' pour revenir en Correction. Retourne True si l’empreinte change.
    """
    before = fingerprint_any(page)
    # On doit être en Correction pour commencer
    if not ensure_correction_view(page):
        return False
    # clic prochaine question
    if not click_next(page):
        return False  # fin d’épreuve
    # On est en QCM : reviens en Correction
    if not ensure_correction_view(page):
        return False
    # attendre une empreinte différente (nouvelle question)
    for _ in range(25):
        page.wait_for_timeout(200)
        dismiss_banners(page)
        after = fingerprint_any(page)
        if after != before:
            return True
    # dernier recours : reload
    try:
        page.reload(wait_until="domcontentloaded")
    except Exception:
        pass
    ensure_correction_view(page)
    for _ in range(25):
        page.wait_for_timeout(200)
        if fingerprint_any(page) != before:
            return True
    return False

# =============== PDF ===============
def render_pdf_html(epreuve_id: str, captured: list, mode: str) -> str:
    header = f"<h1>{'Sujet' if mode=='sujet' else 'Corrigé'} – QCM tHarmo – Épreuve {html_escape(epreuve_id)}</h1>"
    parts = [
        """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>PDF</title>
<style>
@page{size:A4;margin:16mm}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;line-height:1.35}
h1{font-size:18pt;margin:0 0 8mm}
h2{font-size:12pt;margin:8mm 0 4mm}
.qcm{break-inside:avoid;margin-bottom:10mm;padding-bottom:5mm;border-bottom:1px solid #ddd}
.enonce{margin:3mm 0 4mm}
.lines{margin-left:2mm}
.line{margin:2mm 0}
.muted{color:#666;font-size:10pt}
.corr{margin-top:2mm;padding:3mm;background:#f6f8fa;border-left:3px solid #c5e1a5}
.badge{font-weight:700}
</style></head><body>""",
        header,
        f"<p class='muted'>Questions exportées : {len(captured)}</p>",
    ]
    def esc(s):
        return html_escape((s or ""))
    for i, q in enumerate(captured, start=1):
        parts.append(f"<section class='qcm'><h2>{i}. {esc(q['title'])}</h2>")
        if q.get("enonce"):
            parts.append(f"<div class='enonce'><strong>Énoncé</strong> : {esc(q['enonce'])}</div>")
        parts.append("<div class='lines'>")
        for it in q["items"]:
            if mode == "sujet":
                parts.append(f"<div class='line'><strong>{esc(it['letter'])}</strong> — {esc(it['sujet'])}</div>")
            else:
                vf = "✔ Vrai" if it["isTrue"] else ("✖ Faux" if it["isFalse"] else "•")
                corr = f" – {esc(it['correction'])}" if it["correction"] else ""
                rep = f" (Votre réponse : {esc(it['reponse'])})" if it["reponse"] else ""
                parts.append(
                    f"<div class='line'><strong>{esc(it['letter'])}</strong> — {esc(it['sujet'])}"
                    f"<div class='corr'><span class='badge'>{vf}</span>{corr}{rep}</div></div>"
                )
        parts.append("</div></section>")
    parts.append("</body></html>")
    return "".join(parts)

def html_to_pdf_bytes(play, html: str) -> bytes:
    """Génère un PDF via Playwright (contexte séparé)."""
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

# =============== Run ===============
if submitted:
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
                    # désactive les timeouts Playwright (on pilote manuellement)
                    page.set_default_timeout(0)
                    context.set_default_timeout(0)

                    # Login
                    if not try_login(page, base, username, password):
                        st.error("Échec de connexion. Vérifie identifiants.")
                        st.stop()

                    for eid in ids:
                        st.write(f"### Épreuve {eid}")
                        if not start_correction(page, base, eid):
                            st.error("Impossible d’atteindre la vue 'Correction'.")
                            continue

                        captured = []
                        seen = set()  # empreintes pour éviter doublons

                        while True:
                            # Assure la vue Correction pour chaque question
                            if not ensure_correction_view(page):
                                break

                            # Extract via BeautifulSoup
                            data = extract_current(page)
                            if data and data.get("items"):
                                fp = fingerprint_any(page)
                                if fp not in seen:
                                    seen.add(fp)
                                    captured.append(
                                        {
                                            "title": (data.get("title") or "").strip(),
                                            "enonce": html2txt(data.get("enonceHTML") or ""),
                                            "items": data["items"],
                                        }
                                    )
                                    st.write(
                                        f"✓ Capturé {len(captured)} : {captured[-1]['title'] or '(sans titre)'}"
                                    )

                            # Avancer à la question suivante
                            if not go_next_and_reenter_correction(page):
                                break

                        if not captured:
                            st.warning("Aucune question capturée.")
                            continue

                        # Génération PDF
                        html_sujet = render_pdf_html(eid, captured, "sujet")
                        html_corr = render_pdf_html(eid, captured, "corrige")
                        sujet_name = f"qcm_tharmo_{eid}_sujet.pdf"
                        corr_name = f"qcm_tharmo_{eid}_corrige.pdf"

                        try:
                            sujet_bytes = html_to_pdf_bytes(play, html_sujet)
                            corr_bytes = html_to_pdf_bytes(play, html_corr)
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
                            st.error(f"Erreur PDF: {e}")

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

# Informations complémentaires
st.divider()
with st.expander("Notes & Conseils"):
    st.markdown(
        """
- **Identifiants** : ils sont utilisés uniquement pour ouvrir une session le temps de l’export.
- **Respecte les CGU** de tHarmo / Tutorat (usage personnel).
- Si une épreuve ne s’exporte pas, fournis l’**ID** exact (ou l’URL) et réessaie.
"""
    )
