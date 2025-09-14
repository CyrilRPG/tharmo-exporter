# app.py — Streamlit + Playwright — Export tHarmo (Sujet + Corrigé)
# Fonctionne même si "Question suivante" sort de la vue Correction vers la vue QCM :
# on reclique automatiquement "Correction" à chaque question.

import re
import time
from html import unescape as html_unescape, escape as html_escape

import streamlit as st
from playwright.sync_api import sync_playwright, Error as PwError

APP_TITLE = "tHarmo → PDF (Sujet + Corrigé)"
st.set_page_config(page_title=APP_TITLE, layout="centered")
st.title(APP_TITLE)
st.caption("Entrez vos identifiants tHarmo + l’ID d’épreuve. L’appli génère 2 PDF à télécharger.")

# ================= UI =================
with st.form("params"):
    base = st.text_input("Base tHarmo", "https://pass.tharmo.tutotours.fr").strip().rstrip("/")
    username = st.text_input("Email / Identifiant tHarmo", value="", autocomplete="username")
    password = st.text_input("Mot de passe tHarmo", value="", type="password", autocomplete="current-password")
    ids_text = st.text_area(
        "ID(s) d’épreuve (un par ligne ou collez l’URL)",
        placeholder="1914339\nhttps://pass.tharmo.tutotours.fr/banque/qc/entrainement/qcmparqcm/idEpreuve=1914315",
        height=100,
    )
    submitted = st.form_submit_button("Exporter (Sujet + Corrigé)")

# ================= Utils =================
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
    try:
        candidates = [
            'button:has-text("Accepter")','button:has-text("J\'accepte")','button:has-text("OK")',
            'button:has-text("D\'accord")','button:has-text("Compris")','button:has-text("Fermer")',
            '[aria-label="Fermer"]','#didomi-notice-agree-button','button.cookie-accept',
        ]
        for sel in candidates:
            if page.locator(sel).first.count() > 0:
                try: page.locator(sel).first.click()
                except Exception: pass
        page.evaluate("""() => {
            for (const s of ['.cookie', '.modal', '#cookie', '.overlay', '.consent']) {
                document.querySelectorAll(s).forEach(n => {
                    const z = parseInt(getComputedStyle(n).zIndex||'0',10);
                    if (z >= 1000) n.style.display='none';
                });
            }
        }""")
    except Exception:
        pass

def wait_for_any_selector(page, selectors, timeout_ms=None) -> bool:
    def any_present() -> bool:
        dismiss_banners(page)
        for s in selectors:
            try:
                loc = page.locator(s)
                if loc.count() > 0:
                    try:
                        loc.first.wait_for(state="visible")
                        return True
                    except Exception:
                        return True
            except Exception:
                pass
        return False
    if timeout_ms is None:
        while True:
            if any_present(): return True
            page.wait_for_timeout(250)
    else:
        end = time.monotonic() + timeout_ms/1000.0
        while time.monotonic() < end:
            if any_present(): return True
            page.wait_for_timeout(250)
        return False

def try_login(page, base, username, password):
    page.goto(base + "/banque/qc/entrainement/", wait_until="domcontentloaded")
    dismiss_banners(page)
    if not looks_like_login(page):
        return True
    try:
        email_sel = 'input[type="email"], input[name*="mail" i], input[name*="user" i], input[name*="login" i]'
        pwd_sel   = 'input[type="password"]'
        btn_sel   = 'button:has-text("Connexion"), input[type="submit"], button[type="submit"]'

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

def wait_for_correction_href(page, timeout_ms=None):
    def find_href():
        try:
            return page.evaluate("""() => {
                const byId = document.querySelector('#correction');
                if (byId && byId.getAttribute('href'))
                    return new URL(byId.getAttribute('href'), location.origin).toString();
                const cand = Array.from(document.querySelectorAll('a[href]'))
                    .find(a => /\\/banque\\/qc\\/entrainement\\/correction\\/commencer\\//.test(a.getAttribute('href')||''));
                return cand ? new URL(cand.getAttribute('href'), location.origin).toString() : null;
            }""")
        except Exception:
            return None
    if timeout_ms is None:
        while True:
            href = find_href()
            if href: return href
            page.wait_for_timeout(200)
    else:
        end = time.monotonic() + timeout_ms/1000.0
        while time.monotonic() < end:
            href = find_href()
            if href: return href
            page.wait_for_timeout(200)
        return None

def in_correction_view(page) -> bool:
    try:
        # Correction : présence d’Items + bouton "Question suivante" dans un <footer>
        if page.locator("#nextQuestionButton").count() > 0 and page.locator('span.card-title:has-text("Item")').count() > 0:
            return True
        # Titre "Epreuve 1919110" existe en haut de carte en correction
        if page.locator(".card.card-content .card-title:has-text('Epreuve')").count() > 0:
            return True
    except Exception:
        pass
    return False

def ensure_correction_view(page):
    """Si on est en vue QCM, clique le bouton 'Correction' pour revenir en correction."""
    if in_correction_view(page):
        return True
    # En vue QCM : il y a un <a id="correction" href="...">
    if page.locator("#correction").count() > 0:
        try:
            page.locator("#correction").first.click()
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            # fallback évaluation DOM
            try:
                page.evaluate("""() => {
                    const a = document.querySelector('#correction');
                    if (a) a.click();
                }""")
                page.wait_for_load_state("domcontentloaded")
            except Exception:
                pass
    # Attends des artefacts correction
    wait_for_any_selector(
        page,
        selectors=[
            "#nextQuestionButton",
            'span.card-title:has-text("Item")',
            ".card.card-content .card-title:has-text('Epreuve')",
        ],
        timeout_ms=10000,
    )
    return in_correction_view(page)

def start_correction(page, base, epreuve_id):
    url_qcm  = f"{base}/banque/qc/entrainement/qcmparqcm/idEpreuve={epreuve_id}"
    url_corr = f"{base}/banque/qc/entrainement/correction/commencer/fin=0/id={epreuve_id}"

    page.goto(url_qcm, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    dismiss_banners(page)

    href = wait_for_correction_href(page, 10000) or url_corr
    try:
        page.goto(href, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
    except Exception:
        pass
    dismiss_banners(page)
    return ensure_correction_view(page)

# ================== Extraction ==================
def extract_current(page):
    """Extrait depuis la vue Correction UNIQUEMENT (items, verdicts, etc.)."""
    return page.evaluate(
        """() => {
        // carte principale de CORRECTION
        const root = document.querySelector(".card.card-content") || document.querySelector(".card .card-content") || document.querySelector(".card-content");
        if (!root) return null;

        // titre principal
        let mainTitle = "";
        const titles = Array.from(root.querySelectorAll(".card-title, span.card-title"));
        const qcmTitleEl = titles.find(t => /QCM\\s*:/.test((t.textContent||"").trim())) || titles[0];
        mainTitle = (qcmTitleEl?.textContent||"").replace(/\\s+/g," ").trim();

        // énoncé (il est affiché avant les items ou séparé par divider)
        function getEnonceHTMLInCorrection() {
            // en correction, l'énoncé n'est pas explicitement nommé "Énoncé", on le reconstruit à partir des sujets A..E si besoin
            return "";
        }

        // Items A..E
        const itemTitles = Array.from(root.querySelectorAll("span.card-title")).filter(el => /^Item\\s+[A-E]/i.test((el.textContent||"").trim()));
        const items = itemTitles.map(span => {
            const letter = (span.textContent||"").trim().replace(/^Item\\s+/i, "");
            let row = span.nextElementSibling;
            while(row && !row.matches(".row")) {
                if (row.matches("span.card-title")) break;
                row = row.nextElementSibling;
            }
            const cols = row ? Array.from(row.querySelectorAll(":scope > .col")) : [];
            const pick = (col, label) => {
                if (!col) return "";
                const p = col.querySelector("p.justify, p");
                if (!p) return col.textContent || "";
                let html = p.innerHTML || "";
                html = html.replace(/<span[^>]*>\\s*(Sujet|Correction|Votre r[ée]ponse)\\s*<\\/span>\\s*:\\s*/i, "");
                html = html.replace(/<strong[^>]*>\\s*(Sujet|Correction|Votre r[ée]ponse)\\s*<\\/strong>\\s*:\\s*/i, "");
                return html;
            };
            const colSujet = cols[0] || null;
            const colCorr  = cols[1] || null;
            const colRep   = cols[2] || null;

            const sujetHTML = pick(colSujet, "Sujet");
            const corrHTML  = pick(colCorr, "Correction");
            const repHTML   = pick(colRep, "Votre réponse");

            const isTrue = !!(colCorr && colCorr.querySelector(".green-text"));
            const isFalse= !!(colCorr && colCorr.querySelector(".red-text"));

            return { letter, sujetHTML, correctionHTML: corrHTML, reponseHTML: repHTML, isTrue, isFalse };
        });

        return { title: mainTitle, enonceHTML: getEnonceHTMLInCorrection(), items };
    }"""
    )

# ===== Empreinte multi-vues (QCM ou Correction) =====
def _fingerprint_any_view(page) -> str:
    try:
        return page.evaluate(
            """() => {
            // Vue CORRECTION ?
            const corr = document.querySelector('#nextQuestionButton') && document.querySelector('span.card-title');
            if (corr) {
                const title = (document.querySelector('.card.card-content .card-title')?.textContent || '').trim();
                const firstItem = (document.querySelector('span.card-title')?.textContent || '').trim();
                const verdict = (document.querySelector('.green-text, .red-text')?.textContent || '').trim();
                return 'CORR§' + title + '§' + firstItem + '§' + verdict;
            }
            // Vue QCM (question)
            const qcmTitle = (document.querySelector('.saut-ligne .bold')?.textContent || '').trim();
            // lignes A-E
            const lines = Array.from(document.querySelectorAll('.retour-ligne'))
                .slice(0,3).map(x => (x.textContent||'').trim()).join('|');
            // compteur répondu "X/YYY questions répondues"
            const rightCard = (document.querySelector('.panel-title + .card-panel')?.textContent||'').trim();
            return 'QCM§' + qcmTitle + '§' + lines + '§' + rightCard;
        }"""
        ) or ""
    except Exception:
        return ""

# ===== Navigation =====
def go_next_and_reenter_correction(page) -> bool:
    """
    Depuis la vue CORRECTION :
      - clique "Question suivante" (passe en vue QCM),
      - re-clique "Correction" pour la question suivante,
      - attend un vrai changement d'empreinte.
    Retourne True si on est bien sur la correction d'une nouvelle question.
    """
    before = _fingerprint_any_view(page)

    # sécurité : on est en correction ?
    if not in_correction_view(page):
        if not ensure_correction_view(page):
            return False

    # scroll pour activer/afficher le bouton
    try:
        page.evaluate("() => window.scrollTo({top:document.body.scrollHeight, behavior:'instant'})")
    except Exception:
        pass

    # 1) clic "Question suivante" (lien + bouton)
    clicked = False
    for sel in ["#nextQuestionButton", "a#nextQuestion", 'a:has-text("Question suivante")', 'button:has-text("Question suivante")']:
        if page.locator(sel).count() > 0:
            try:
                page.locator(sel).first.click()
                page.wait_for_load_state("domcontentloaded")
                clicked = True
                break
            except Exception:
                pass
    if not clicked:
        # plus de "suivante" => fin d'épreuve
        return False

    # 2) on est maintenant en VUE QCM ; re-cliquer "Correction"
    if not ensure_correction_view(page):
        return False

    # 3) attendre un vrai changement d’empreinte par rapport à "before"
    for _ in range(20):
        page.wait_for_timeout(200)
        dismiss_banners(page)
        after = _fingerprint_any_view(page)
        if after and after != before:
            return True

    # Si l'empreinte n'a pas changé, tenter un reload et re-check
    try:
        page.reload(wait_until="domcontentloaded")
    except Exception:
        pass
    ensure_correction_view(page)
    for _ in range(20):
        page.wait_for_timeout(200)
        after = _fingerprint_any_view(page)
        if after and after != before:
            return True

    return False

# ====== Rendu PDF ======
def render_pdf_html(epreuve_id: str, captured: list, mode: str) -> str:
    header = f"<h1>{'Sujet' if mode=='sujet' else 'Corrigé'} – QCM tHarmo – Épreuve {html_escape(epreuve_id)}</h1>"
    parts = ["""
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
</style></head><body>""", header, f"<p class='muted'>Questions exportées : {len(captured)}</p>"]
    def esc(s): return html_escape((s or ""))
    for i, q in enumerate(captured, start=1):
        parts.append(f"<section class='qcm'><h2>{i}. {esc(q['title'])}</h2>")
        if q["enonce"]:
            parts.append(f"<div class='enonce'><strong>Énoncé</strong> : {esc(q['enonce'])}</div>")
        parts.append("<div class='lines'>")
        for it in q["items"]:
            if mode == "sujet":
                parts.append(f"<div class='line'><strong>{esc(it['letter'])}</strong> — {esc(it['sujet'])}</div>")
            else:
                vf = "✔ Vrai" if it["isTrue"] else ("✖ Faux" if it["isFalse"] else "•")
                corr = f" – {esc(it['correction'])}" if it["correction"] else ""
                rep  = f" (Votre réponse : {esc(it['reponse'])})" if it["reponse"] else ""
                parts.append(f"<div class='line'><strong>{esc(it['letter'])}</strong> — {esc(it['sujet'])}"
                             f"<div class='corr'><span class='badge'>{vf}</span>{corr}{rep}</div></div>")
        parts.append("</div></section>")
    parts.append("</body></html>")
    return "".join(parts)

def html_to_pdf_bytes(play, html: str) -> bytes:
    browser = play.chromium.launch(headless=True)
    try:
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        p = ctx.new_page()
        p.set_content(html, wait_until="domcontentloaded")
        p.emulate_media(media="print")
        pdf_bytes = p.pdf(
            format="A4", print_background=True,
            margin={"top":"10mm","right":"10mm","bottom":"12mm","left":"10mm"}
        )
        ctx.close()
        return pdf_bytes
    finally:
        browser.close()

# ================== Run ==================
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
                    # pas de timeouts globaux
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
                        seen = set()

                        # boucle questions
                        while True:
                            # s'assurer qu'on est bien en correction pour cette question
                            if not ensure_correction_view(page):
                                break

                            # extraire
                            data = extract_current(page)
                            if data and data.get("items"):
                                fp = _fingerprint_any_view(page)
                                if fp and fp not in seen:
                                    seen.add(fp)
                                    captured.append({
                                        "title": (data.get("title") or "").strip(),
                                        "enonce": html2txt(data.get("enonceHTML") or ""),
                                        "items": [{
                                            "letter": it.get("letter"),
                                            "sujet": html2txt(it.get("sujetHTML") or ""),
                                            "correction": html2txt(it.get("correctionHTML") or ""),
                                            "reponse": html2txt(it.get("reponseHTML") or ""),
                                            "isTrue": bool(it.get("isTrue")),
                                            "isFalse": bool(it.get("isFalse")),
                                        } for it in data.get("items", [])]
                                    })
                                    st.write(f"✓ Capturé {len(captured)} : {captured[-1]['title'] or '(sans titre)'}")

                            # avancer (question suivante → QCM → Correction)
                            if not go_next_and_reenter_correction(page):
                                break

                        if not captured:
                            st.warning("Aucune question capturée.")
                            continue

                        # PDFs
                        html_sujet = render_pdf_html(eid, captured, "sujet")
                        html_corr  = render_pdf_html(eid, captured, "corrige")
                        sujet_name = f"qcm_tharmo_{eid}_sujet.pdf"
                        corr_name  = f"qcm_tharmo_{eid}_corrige.pdf"

                        try:
                            sujet_bytes = html_to_pdf_bytes(play, html_sujet)
                            corr_bytes  = html_to_pdf_bytes(play, html_corr)
                            st.success("PDF générés.")
                            st.download_button("⬇️ Télécharger Sujet", data=sujet_bytes, file_name=sujet_name, mime="application/pdf")
                            st.download_button("⬇️ Télécharger Corrigé", data=corr_bytes, file_name=corr_name, mime="application/pdf")
                        except Exception as e:
                            st.error(f"Erreur PDF: {e}")

                finally:
                    try: context.close()
                    except Exception: pass
                    browser.close()
        except PwError as e:
            st.error(f"Erreur Playwright : {e}")
        except Exception as e:
            st.error(f"Erreur inattendue : {e}")

st.divider()
with st.expander("Notes & Conseils"):
    st.markdown("""
- **Identifiants** : ils sont utilisés uniquement pour ouvrir une session le temps de l’export.
- **Respecte les CGU** de tHarmo / Tutorat (usage personnel).
- Si une épreuve ne s’exporte pas, fournis l’**ID** exact (ou l’URL) et réessaie.
""")
