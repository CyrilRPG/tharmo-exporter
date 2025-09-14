# app.py — Streamlit + Playwright (cloud) — export Sujet + Corrigé depuis tHarmo
# Ouvre cette appli depuis l'iPad (URL Render) — aucune install sur l'iPad.

import re
import io
import time
from html import unescape as html_unescape, escape as html_escape
from pathlib import Path

import streamlit as st
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout, Error as PwError


APP_TITLE = "tHarmo → PDF (Sujet + Corrigé)"
st.set_page_config(page_title=APP_TITLE, layout="centered")
st.title(APP_TITLE)
st.caption("Entrez vos identifiants tHarmo + l’ID d’épreuve. L’appli génère 2 PDF à télécharger.")

# ====== UI ======
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

# ====== Utils ======
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
    # déduplique en conservant l’ordre
    dedup, seen = [], set()
    for x in out:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup


def html2txt(html: str) -> str:
    """Convertit un fragment HTML en texte lisible pour PDF (simple)."""
    if not html:
        return ""
    # Exposants simples <sup>2</sup> -> ^2
    html = re.sub(r"(?is)<\s*sup\s*>(.*?)</\s*sup\s*>", lambda m: "^" + m.group(1), html)
    # Supprimer les balises en conservant le texte
    tmp = re.sub(r"(?is)<[^>]+>", "", html)
    # Décoder les entités HTML
    tmp = html_unescape(tmp)
    # Normaliser les espaces
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
    """Ferme quelques overlays/bannières susceptibles de bloquer les clics."""
    try:
        # Cookies / consentements / modales communes
        candidates = [
            'button:has-text("Accepter")',
            'button:has-text("J\'accepte")',
            'button:has-text("OK")',
            'button:has-text("D\'accord")',
            'button:has-text("Compris")',
            'button:has-text("Fermer")',
            '[aria-label="Fermer"]',
            'button.cookie-accept',
            '#didomi-notice-agree-button',
        ]
        for sel in candidates:
            if page.locator(sel).first.count() > 0:
                try:
                    page.locator(sel).first.click(timeout=1000)
                except Exception:
                    pass
        # Retirer overlays plein écran connus
        page.evaluate(
            """() => {
                for (const s of ['.cookie', '.modal', '#cookie', '.overlay', '.consent']) {
                    document.querySelectorAll(s).forEach(n => {
                        if (getComputedStyle(n).zIndex > '1000') n.style.display = 'none';
                    });
                }
            }"""
        )
    except Exception:
        pass


def wait_for_any_selector(page, selectors, timeout_ms=20000) -> bool:
    """Attend qu'au moins un sélecteur soit présent/visible."""
    end = time.monotonic() + timeout_ms / 1000.0
    last_err = None
    while time.monotonic() < end:
        dismiss_banners(page)
        for s in selectors:
            try:
                loc = page.locator(s)
                if loc.count() > 0:
                    # visible si possible, sinon présent
                    try:
                        loc.first.wait_for(state="visible", timeout=500)
                        return True
                    except Exception:
                        return True
            except Exception as e:
                last_err = e
        page.wait_for_timeout(250)
    if last_err:
        return False
    return False


def try_login(page, base, username, password, timeout_ms=30000):
    """Se rend sur une page protégée et se connecte si nécessaire."""
    # Aller sur une page protégée → redirection login
    page.goto(base + "/banque/qc/entrainement/", wait_until="domcontentloaded")
    dismiss_banners(page)
    if not looks_like_login(page):
        return True  # déjà connecté

    # Heuristiques de formulaire de login
    try:
        email_sel = 'input[type="email"], input[name*="mail" i], input[name*="user" i], input[name*="login" i]'
        pwd_sel = 'input[type="password"]'
        btn_sel = 'button:has-text("Connexion"), input[type="submit"], button[type="submit"]'

        page.locator(email_sel).first.fill(username, timeout=5000)
        page.locator(pwd_sel).first.fill(password, timeout=5000)
        if page.locator(btn_sel).count() > 0:
            page.locator(btn_sel).first.click()
        else:
            page.keyboard.press("Enter")

        page.wait_for_url(lambda u: re.search(r"(login|connexion|auth)", u, re.I) is None, timeout=timeout_ms)
        # petite marge
        page.wait_for_load_state("domcontentloaded")
        dismiss_banners(page)
        return True
    except PwTimeout:
        return False
    except Exception:
        return False


def wait_for_correction_href(page, timeout_ms=15000):
    """Repère le lien vers la correction sur la page d'épreuve."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            href = page.evaluate(
                """() => {
                const byId = document.querySelector('#correction');
                if (byId && byId.getAttribute('href'))
                    return new URL(byId.getAttribute('href'), location.origin).toString();
                const cand = Array.from(document.querySelectorAll('a[href]'))
                  .find(a => /\\/banque\\/qc\\/entrainement\\/correction\\/commencer\\//.test(a.getAttribute('href')||''));
                return cand ? new URL(cand.getAttribute('href'), location.origin).toString() : null;
            }"""
            )
            if href:
                return href
        except Exception:
            pass
        page.wait_for_timeout(250)
    return None


def in_correction_view(page) -> bool:
    """Heuristiques pour savoir si on est dans la vue de correction."""
    try:
        next_count = page.locator('#nextQuestionButton, a#nextQuestion, button:has-text("Question suivante"), a:has-text("Question suivante")').count()
        item_count = page.locator('span.card-title:has-text("Item")').count()
        title_el = page.locator(".card .card-content .card-title").first
        title = (title_el.text_content() or "").strip() if title_el else ""
        return (next_count > 0 and item_count > 0) or (re.search(r"Epreuve\s+\d+", title or "", re.I) is not None)
    except Exception:
        return False


def start_correction(page, base, epreuve_id):
    """Ouvre la page d’épreuve et lance/affiche la correction."""
    url_qcm = f"{base}/banque/qc/entrainement/qcmparqcm/idEpreuve={epreuve_id}"
    url_corr = f"{base}/banque/qc/entrainement/correction/commencer/fin=0/id={epreuve_id}"

    page.goto(url_qcm, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    dismiss_banners(page)

    href = wait_for_correction_href(page, 15000) or url_corr
    try:
        page.goto(href, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
    except Exception:
        # Dernier recours : clic DOM sur #correction
        try:
            page.evaluate(
                """() => {
                const a = document.querySelector('#correction');
                if (a) a.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            }"""
            )
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

    dismiss_banners(page)
    # Attendre que la structure de correction apparaisse
    got = wait_for_any_selector(
        page,
        selectors=[
            ".card .card-content .card-title",
            'span.card-title:has-text("Item")',
            "#nextQuestionButton",
            "a#nextQuestion",
            'a:has-text("Question suivante")',
        ],
        timeout_ms=20000,
    )
    return got or in_correction_view(page)


def extract_current(page):
    """Extrait l'énoncé + Items A..E de la question courante dans la page de correction."""
    return page.evaluate(
        """() => {
      const cards = Array.from(document.querySelectorAll(".card .card-content, .card.card-content, .card-content"));
      let container =
           cards.find(c => Array.from(c.querySelectorAll("span.card-title")).some(s => /^Item\\s+[A-E]/i.test((s.textContent||"").trim())))
        || cards.find(c => Array.from(c.querySelectorAll(".card-title")).some(t => /enonc/i.test((t.textContent||""))))
        || cards[cards.length-1]
        || null;
      if(!container) return null;

      const titles = Array.from(container.querySelectorAll(".card-title, span.card-title"));
      const mainTitle = (titles[0]?.textContent||"").replace(/\\s+/g," ").trim();

      function getEnonceHTML(){
        const h = titles.find(t=>/enonc/i.test((t.textContent||"")));
        if(!h) return "";
        let html=""; let n=h.nextSibling;
        while(n){
          if(n.nodeType===1){
            const el=n;
            if(el.matches(".divider.with-margin")) break;
            if(el.matches("span.card-title") && /^Item\\s+[A-E]/i.test((el.textContent||""))) break;
            html += " " + (el.innerHTML || "");
          }else if(n.nodeType===3){
            html += " " + n.textContent;
          }
          n = n.nextSibling;
        }
        return html;
      }

      const itemSpans = titles.filter(s => /^Item\\s+[A-E]/i.test((s.textContent||"").trim()));
      const items = itemSpans.map(span=>{
        const letter=(span.textContent||"").trim().replace(/^Item\\s+/i,"");
        let row=span.nextElementSibling;
        while(row && !row.matches(".row")){
          if(row.matches("span.card-title")) break;
          row=row.nextElementSibling;
        }
        const cols=row?Array.from(row.querySelectorAll(":scope > .col, :scope > [class*=col-], :scope > [class*=col]")):[];
        const byLabel=(root,label)=>{
          if(!root) return "";
          const bolds=Array.from(root.querySelectorAll("p.justify span.bold, p span.bold, strong"));
          for(const b of bolds){
            const t=(b.textContent||"").trim().toLowerCase();
            if(t.startsWith(label)){
              let html=b.parentElement?.innerHTML||"";
              html=html.replace(/<span[^>]*>\\s*(Sujet|Correction|Votre r[ée]ponse)\\s*<\\/span>\\s*:\\s*/i,"");
              html=html.replace(/<strong[^>]*>\\s*(Sujet|Correction|Votre r[ée]ponse)\\s*<\\/strong>\\s*:\\s*/i,"");
              return html;
            }
          }
          return "";
        };
        const sujetHTML = byLabel(cols[0],"sujet");
        const corrHTML  = byLabel(cols[1],"correction");
        const repHTML   = byLabel(cols[2],"votre r");

        const corrCol=cols[1]||null;
        const isTrue = !!(corrCol && (corrCol.querySelector(".green-text, .text-success")));
        const isFalse= !!(corrCol && (corrCol.querySelector(".red-text, .text-danger")));
        const corrPara = corrCol?.querySelector("p.justify, p")?.innerHTML || corrHTML;

        return { letter, sujetHTML, correctionHTML: corrPara || "", reponseHTML: repHTML || "", isTrue, isFalse };
      });

      return {title: mainTitle, enonceHTML: getEnonceHTML(), items};
    }"""
    )


def go_next(page) -> bool:
    """Avance à la question suivante de manière robuste. Retourne True si la page a changé."""
    def signature():
        try:
            title = page.locator(".card .card-content .card-title").first.text_content() or ""
        except Exception:
            title = ""
        try:
            idhint = page.evaluate(
                """() => {
              const n = document.querySelector('[id^="modalInfo"],[id^="modalSignalement"],[id^="marqueIcon"],[id^="marqueButton"]');
              return n?.id || "";
            }"""
            )
        except Exception:
            idhint = ""
        return (title.strip(), idhint)

    before = signature()
    try:
        page.evaluate("() => window.scrollTo({top:document.body.scrollHeight, behavior:'instant'})")
    except Exception:
        pass

    # Essais progressifs
    tries = [
        lambda: page.locator("#nextQuestionButton").first.click(),
        lambda: page.locator("a#nextQuestion").first.click(),
        lambda: page.locator('a:has-text("Question suivante")').first.click(),
        lambda: page.locator('button:has-text("Question suivante")').first.click(),
        lambda: (lambda href: page.goto(href))(page.evaluate(
            """() => {
            const a = document.querySelector('#nextQuestion') || document.querySelector('#nextQuestionButton')?.closest('a');
            const href = a?.getAttribute('href') || null;
            if (!href) return null;
            return href.startsWith('http') ? href : (location.origin + (href.startsWith('/')? href : '/' + href));
        }"""
        )),
    ]
    for t in tries:
        try:
            t()
            page.wait_for_load_state("domcontentloaded", timeout=4000)
        except Exception:
            pass
        # detect change
        after = signature()
        if after != before:
            return True
        dismiss_banners(page)
        page.wait_for_timeout(150)
    return False


def render_pdf_html(epreuve_id: str, captured: list, mode: str) -> str:
    # mode: "sujet" or "corrige"
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
    def esc(s): 
        return html_escape((s or ""))

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
                rep = f" (Votre réponse : {esc(it['reponse'])})" if it["reponse"] else ""
                parts.append(
                    f"<div class='line'><strong>{esc(it['letter'])}</strong> — {esc(it['sujet'])}"
                    f"<div class='corr'><span class='badge'>{vf}</span>{corr}{rep}</div></div>"
                )
        parts.append("</div></section>")
    parts.append("</body></html>")
    return "".join(parts)


def html_to_pdf_bytes(play, html: str) -> bytes:
    """Imprime du HTML en PDF via un contexte Playwright séparé (plus stable)."""
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


# ====== Run ======
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
                    page.set_default_timeout(20000)

                    # Login
                    if not try_login(page, base, username, password):
                        st.error("Échec de connexion. Vérifie identifiants.")
                        st.stop()

                    for eid in ids:
                        st.write(f"### Épreuve {eid}")
                        ok = start_correction(page, base, eid)
                        if not ok:
                            st.error("Impossible d’atteindre la vue 'Correction'.")
                            continue

                        captured = []
                        seen = set()
                        # boucle jusqu’à la fin
                        consecutive_timeouts = 0
                        while True:
                            # Attendre un conteneur raisonnable de contenu
                            ready = wait_for_any_selector(
                                page,
                                selectors=[
                                    ".card .card-content",
                                    ".card-content",
                                    'span.card-title:has-text("Item")',
                                ],
                                timeout_ms=20000,
                            )
                            if not ready:
                                consecutive_timeouts += 1
                                if consecutive_timeouts >= 2:
                                    # rechargement de secours
                                    try:
                                        page.reload(wait_until="domcontentloaded")
                                        page.wait_for_load_state("networkidle")
                                        consecutive_timeouts = 0
                                        dismiss_banners(page)
                                    except Exception:
                                        break
                                else:
                                    continue

                            data = extract_current(page)
                            if data and data.get("items"):
                                key = f"{data.get('title','').strip()}::{(data.get('enonceHTML') or '')[:120]}"
                                if key not in seen:
                                    seen.add(key)
                                    captured.append(
                                        {
                                            "title": (data.get("title") or "").strip(),
                                            "enonce": html2txt(data.get("enonceHTML") or ""),
                                            "items": [
                                                {
                                                    "letter": it.get("letter"),
                                                    "sujet": html2txt(it.get("sujetHTML") or ""),
                                                    "correction": html2txt(it.get("correctionHTML") or ""),
                                                    "reponse": html2txt(it.get("reponseHTML") or ""),
                                                    "isTrue": bool(it.get("isTrue")),
                                                    "isFalse": bool(it.get("isFalse")),
                                                }
                                                for it in data.get("items", [])
                                            ],
                                        }
                                    )
                                    st.write(f"✓ Capturé {len(captured)} : {captured[-1]['title'] or '(sans titre)'}")

                            # avancer
                            if not go_next(page):
                                break

                        if not captured:
                            st.warning("Aucune question capturée.")
                            continue

                        # Générer les 2 PDF
                        html_sujet = render_pdf_html(eid, captured, "sujet")
                        html_corr = render_pdf_html(eid, captured, "corrige")
                        sujet_name = f"qcm_tharmo_{eid}_sujet.pdf"
                        corr_name = f"qcm_tharmo_{eid}_corrige.pdf"

                        try:
                            # PDF via un contexte séparé (évite conflits d’impression)
                            sujet_bytes = html_to_pdf_bytes(play, html_sujet)
                            corr_bytes = html_to_pdf_bytes(play, html_corr)
                            st.success("PDF générés.")
                            st.download_button("⬇️ Télécharger Sujet", data=sujet_bytes, file_name=sujet_name, mime="application/pdf")
                            st.download_button("⬇️ Télécharger Corrigé", data=corr_bytes, file_name=corr_name, mime="application/pdf")
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

st.divider()
with st.expander("Notes & Conseils"):
    st.markdown("""
- **Identifiants** : ils sont utilisés uniquement pour ouvrir une session le temps de l’export.
- **Respecte les CGU** de tHarmo / Tutorat (usage personnel).
- Si une épreuve ne s’exporte pas, fournis l’**ID** exact (ou l’URL) et réessaie.
""")
