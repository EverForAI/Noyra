import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, dirname } from "node:path";
import { createHash } from "node:crypto";
import { content, shared } from "./content.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const site = join(here, "..", "site");
const base = "https://everforai.github.io/Noyra/";
const esc = (value) =>
    String(value).replace(
        /[&<>"']/g,
        (c) =>
            ({
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;",
            })[c],
    );
const icon = (name) =>
    readFileSync(join(here, "icons", `${name}.svg`), "utf8").replace(
        "<svg",
        '<svg aria-hidden="true" focusable="false"',
    );
const link = (label, url, classes = "text-link", symbol = "arrow-up-right") =>
    `<a class="${classes}" href="${esc(url)}">${esc(label)}${icon(symbol)}</a>`;
const tag = (text) => `<p class="eyebrow">${esc(text)}</p>`;
const lines = (parts) =>
    parts.map((part) => `<span>${esc(part)}</span>`).join("");

function render(locale, page = "home") {
    const c = content[locale];
    const isEn = locale === "en";
    const research = page === "research";
    const sponsor = page === "sponsor";
    const section = page === "home" ? "" : `${page}/`;
    const path = `${isEn ? "en/" : ""}${section}`;
    const root = "../".repeat((isEn ? 1 : 0) + (page === "home" ? 0 : 1));
    const home = `${root}${isEn ? "en/" : ""}index.html`;
    const study = `${root}${isEn ? "en/" : ""}research/index.html`;
    const support = `${root}${isEn ? "en/" : ""}sponsor/index.html`;
    const chinese = `${root}${section}index.html`;
    const english = `${root}en/${section}index.html`;
    const asset = (name) => {
        const version = createHash("sha256")
            .update(readFileSync(join(site, "assets", name)))
            .digest("hex")
            .slice(0, 12);
        return `${root}assets/${name}?v=${version}`;
    };
    const nav = c.nav
        .map(
            (item, i) =>
                `<a href="${i === 3 ? study : i === 4 ? support : `${page !== "home" ? home : ""}#${["philosophy", "architecture", "vision"][i]}`}"${(research && i === 3) || (sponsor && i === 4) ? ' aria-current="page"' : ""}>${esc(item)}</a>`,
        )
        .join("");
    const header = `<a class="skip-link" href="#main">${esc(c.skip)}</a>
  <header class="site-header"><div class="header-inner">
    <a class="brand" href="${home}" aria-label="Noyra"><span class="brand-glyph" aria-hidden="true">N</span>Noyra<span class="brand-note">RESEARCH</span></a>
    <nav class="desktop-nav" aria-label="${isEn ? "Main navigation" : "主导航"}">${nav}</nav>
    <div class="header-actions"><div class="languages" aria-label="${isEn ? "Language" : "语言"}"><a href="${chinese}" lang="zh-CN"${!isEn ? ' aria-current="page"' : ""}>中文</a><span>/</span><a href="${english}" lang="en"${isEn ? ' aria-current="page"' : ""}>EN</a></div>
    ${link("GitHub", shared.repo, "github-link")}<button class="menu-toggle" aria-expanded="false" aria-controls="mobile-navigation" aria-label="${esc(c.menu)}" data-open-label="${esc(c.menu)}" data-close-label="${esc(c.close)}">${icon("menu")}${icon("x")}</button></div>
  </div><nav id="mobile-navigation" class="mobile-nav" aria-label="${isEn ? "Mobile navigation" : "移动导航"}">${nav}${link("GitHub", shared.repo)}</nav></header>`;
    const footer = `<footer class="footer"><div class="wrap footer-top"><div><a class="footer-brand" href="${home}">Noyra</a><p>${esc(c.footerLine)}</p></div><nav aria-label="${isEn ? "Footer" : "页脚"}">${link(c.footerResearch, study)}${link(c.footerDocs, `${shared.docs}README.md`)}${link(c.footerSafety, `${shared.repo}/security/advisories/new`)}</nav></div><div class="wrap footer-bottom"><p>© 2026 Jaxon Grey / EverForAI</p><a href="${shared.repo}/blob/main/LICENSE">Apache-2.0</a><p>${esc(c.footerNote)}</p></div></footer>`;
    const evidence = `<div class="verification"><div><span class="status-dot" aria-hidden="true"></span>${esc(c.verification)}<strong>${esc(c.verificationBody)}</strong></div>${link(c.verificationLink, shared.ci)}</div>`;
    const picture = (name, cls, eager = false) =>
        `<picture class="${cls}"><source media="(max-width: 640px)" srcset="${asset(`${name}-mobile.webp`)}"><img src="${asset(`${name}.webp`)}" alt="" width="1536" height="1024" ${eager ? 'fetchpriority="high"' : 'loading="lazy"'} decoding="async"></picture>`;
    const art = (name, eager = false) => picture(name, "section-art", eager);
    const cta = `<section class="closing scenic">${art("memory")}<div class="wrap">${tag(c.ctaTag)}<h2>${esc(c.ctaTitle)}</h2><p>${esc(c.ctaBody)}</p><div class="button-row">${link(c.source, shared.repo, "button button-primary")}${link(c.download, shared.release, "button button-outline")}</div></div></section>`;
    let main;
    if (page === "home") {
        main = `<section class="hero">
      ${picture("continuity", "hero-image", true)}
      <div class="wrap hero-content"><p class="hero-status"><span class="status-dot" aria-hidden="true"></span>${esc(c.releaseLabel)}<span class="mono">NCAS</span></p>
      <h1>Noyra<span class="wordmark-period">.</span></h1><h2 class="hero-statement">${lines(c.heroTitle)}</h2>
      <p class="hero-description">${esc(c.heroBody)}</p><div class="button-row">${link(c.primary, "#philosophy", "button button-primary", "arrow-right")}${link(c.source, shared.repo, "button button-glass")}</div></div>
      <div class="wrap hero-bottom"><span class="mono">CONTINUITY, BY DESIGN.</span><a href="#philosophy" class="scroll-down" aria-label="${esc(c.primary)}">${icon("arrow-down")}</a></div>
    </section>
    <div class="signal-band"><div class="wrap signal-grid">${c.signals.map(([name, desc], i) => `<div><span class="signal-number">0${i + 1}</span><div><h3>${esc(name)}</h3><p>${esc(desc)}</p></div></div>`).join("")}</div></div>
    <section id="philosophy" class="section philosophy scenic">${art("memory")}<div class="wrap">${tag(c.ideaTag)}<div class="split"><h2>${lines(c.ideaTitle)}</h2><div><p class="lead">${esc(c.ideaBody)}</p><p>${esc(c.ideaDetail)}</p><p class="side-note">${esc(c.ideaNote)}</p></div></div></div></section>
    <section id="architecture" class="section architecture scenic">${art("substrate")}<div class="wrap">${tag(c.architectureTag)}<div class="section-heading"><h2>${esc(c.architectureTitle)}</h2><p>${esc(c.architectureBody)}</p></div>
    <div class="architecture-tool"><div class="tool-top"><span class="mono">THE CONTINUITY LOOP</span><span>${esc(c.loopCaption)}</span></div><ol class="loop">${c.loop.map(([title, desc], i) => `<li><span class="loop-number">0${i + 1}</span><h3>${esc(title)}</h3><p>${esc(desc)}</p>${i < 3 ? icon("arrow-right") : icon("rotate-ccw")}</li>`).join("")}</ol><div class="supervisor"><span>${icon("shield-check")}${esc(c.supervisor)}</span><span>${esc(c.boundary)}</span></div></div>
    <div class="domain-grid">${c.domains.map(([title, body], i) => `<article><span class="domain-index">[ 0${i + 1} ]</span><h3>${esc(title)}</h3><p>${esc(body)}</p></article>`).join("")}</div></div></section>
    <section id="capabilities" class="section capability-section scenic">${art("memory")}<div class="wrap">${tag(c.capabilityTag)}<div class="section-heading"><h2>${esc(c.capabilityTitle)}</h2><p>${esc(c.capabilityBody)}</p></div><div class="table-wrap" role="region" aria-label="${esc(c.capabilityTitle)}" tabindex="0"><table><thead><tr>${c.statusHeaders.map((h) => `<th scope="col">${esc(h)}</th>`).join("")}</tr></thead><tbody>${c.capabilities.map(([domain, implementation]) => `<tr><th scope="row">${esc(domain)}</th><td>${esc(implementation)}</td></tr>`).join("")}</tbody></table></div>${evidence}</div></section>
    <section id="vision" class="section vision"><div class="wrap">${tag(c.visionTag)}<div class="section-heading"><h2>${lines(c.visionTitle)}</h2><p>${esc(c.visionBody)}</p></div><figure class="vision-image">${picture("horizon", "horizon-image")}</figure><div class="vision-list">${c.visions.map(([title, body, meta], i) => `<article><span class="vision-number">0${i + 1}</span><h3>${esc(title)}<small>${esc(meta)}</small></h3><p>${esc(body)}</p><span class="vision-arrow" aria-hidden="true">${icon("arrow-up-right")}</span></article>`).join("")}</div></div></section>
    <section class="section roadmap scenic">${art("pathway")}<div class="wrap">${tag(c.roadmapTag)}<h2>${esc(c.roadmapTitle)}</h2><ol class="roadmap-grid">${c.roadmap.map(([state, title, body], i) => `<li><div class="roadmap-marker"><span>${i + 1}</span><p>${esc(state)}</p></div><h3>${esc(title)}</h3><p>${esc(body)}</p></li>`).join("")}</ol></div></section>
    <section class="section faq scenic">${art("memory")}<div class="wrap split"><h2>${esc(c.faqTitle)}</h2><div>${c.faqs.map(([q, a]) => `<details><summary>${esc(q)}${icon("plus")}</summary><p>${esc(a)}</p></details>`).join("")}</div></div></section>${cta}`;
    } else if (research) {
        main = `<section class="research-hero scenic">${art("memory", true)}<div class="wrap">${tag("NOYRA / RESEARCH")}<h1>${esc(c.researchTitle)}</h1><p class="research-intro">${esc(c.researchIntro)}</p><p>${esc(c.researchBody)}</p>${link(c.positioningLink, `${shared.docs}research/2026-10-23-positioning.md`)}<div class="research-tabs"><a href="#architecture-topics">${esc(c.researchTopicLink)}${icon("arrow-down")}</a><a href="#evidence">L1 / L2 / L3${icon("arrow-down")}</a></div></div></section>
    <section id="architecture-topics" class="section criteria-section scenic">${art("memory")}<div class="wrap">${tag(c.criteriaTag)}<h2>${esc(c.criteriaTitle)}</h2><div class="criteria-grid">${c.criteria.map(([title, body], i) => `<article><span class="criterion-id">0${i + 1}</span><h3>${esc(title)}</h3><p>${esc(body)}</p></article>`).join("")}</div>${link(c.definitionLink, `${shared.docs}research/non-command-oriented-subject.md`)}</div></section>
    <section id="evidence" class="section evidence-section scenic">${art("substrate")}<div class="wrap">${tag("02 / EVIDENCE")}<h2>${esc(c.evidenceTitle)}</h2><div class="evidence-grid">${c.evidence.map(([id, title, body]) => `<article><span>${id}</span><h3>${esc(title)}</h3><p>${esc(body)}</p></article>`).join("")}</div>${evidence}</div></section>
    <section id="collaboration" class="section collaboration-section scenic">${art("memory")}<div class="wrap split"><h2>${esc(c.collaborationTitle)}</h2><div><p class="lead">${esc(c.collaborationBody)}</p><p>${esc(c.collaborationBody2)}</p>${link(c.contributeResearch, `${shared.repo}/issues`)}</div></div></section>${cta}`;
    }
    else {
        main = `<section class="sponsor-hero scenic">${art("memory", true)}<div class="wrap sponsor-content">${tag("NOYRA / SUPPORT")}<h1>${esc(c.sponsorTitle)}</h1><p class="sponsor-intro">${esc(c.sponsorIntro)}</p>${link(c.sponsorButton, shared.sponsor, "button button-primary")}<p class="sponsor-notice">${esc(c.sponsorNotice)}<a href="${esc(shared.sponsor)}">${esc(shared.sponsor)}</a></p></div></section>
    <section class="section sponsor-thanks"><div class="wrap sponsor-content">${tag("ACKNOWLEDGMENTS")}<h2>${esc(c.sponsorThanks)}</h2><p>${esc(c.sponsorEmpty)}</p></div></section>
    <section class="sponsor-context"><div class="wrap sponsor-content"><p>${esc(c.researchBody)}</p>${link(c.download, shared.release)}</div></section>`;
    }
    const title = page === "home" ? c.title : `${sponsor ? c.sponsorTitle : c.researchTitle} · ${c.title}`;
    return `<!doctype html>
<html lang="${c.lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="strict-origin-when-cross-origin"><meta name="theme-color" content="#11151b">
<title>${esc(title)}</title><meta name="description" content="${esc(c.description)}"><link rel="canonical" href="${base}${path}">
<link rel="alternate" hreflang="zh-CN" href="${base}${section}"><link rel="alternate" hreflang="en" href="${base}en/${section}"><link rel="alternate" hreflang="x-default" href="${base}${section}">
<meta property="og:type" content="website"><meta property="og:title" content="${esc(title)}"><meta property="og:description" content="${esc(c.description)}"><meta property="og:url" content="${base}${path}"><meta property="og:image" content="${base}assets/social.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630"><meta property="og:image:alt" content="${esc(title)}"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="${base}assets/social.png"><meta name="twitter:image:alt" content="${esc(title)}">
<link rel="icon" type="image/png" href="${asset("favicon.png")}"><link rel="stylesheet" href="${asset("site.css")}"><script src="${asset("site.js")}" defer></script>
</head><body${research ? ' class="research-page"' : ""}>${header}<main id="main">${main}</main>${footer}</body></html>\n`;
}

for (const locale of ["zh", "en"]) {
    for (const page of ["home", "research", "sponsor"]) {
        const folder = join(
            site,
            locale === "en" ? "en" : "",
            page === "home" ? "" : page,
        );
        mkdirSync(folder, { recursive: true });
        writeFileSync(join(folder, "index.html"), render(locale, page));
    }
}
writeFileSync(join(site, ".nojekyll"), "");
writeFileSync(
    join(site, "robots.txt"),
    `User-agent: *\nAllow: /\nSitemap: ${base}sitemap.xml\n`,
);
writeFileSync(
    join(site, "sitemap.xml"),
    `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${["", "en/", "research/", "en/research/", "sponsor/", "en/sponsor/"].map((path) => `<url><loc>${base}${path}</loc></url>`).join("")}</urlset>\n`,
);
console.log("Built six bilingual static pages.");
