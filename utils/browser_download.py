#!/usr/bin/env python3
"""
Browser-Based PDF Download (Playwright)
========================================
Cloudflare/Akamai-protected publisher sites에서 기관 접근(학교 Google 계정 등)을
활용하여 PDF를 다운로드한다. Python requests가 403을 받는 사이트에 대한 최종 fallback.

Requirements:
    pip install playwright
    playwright install chromium

Usage (standalone):
    python -m utils.browser_download --doi "10.1073/pnas.2021531118" --output ./pdfs/

Usage (in pipeline):
    from utils.browser_download import browser_download_pdfs
    results = browser_download_pdfs(dois, download_dir, headless=False)

Supported publishers:
    - PNAS (pnas.org) — epdf viewer → JS fetch download
    - Royal Society (royalsocietypublishing.org) — PDF link → tokenized URL
    - Nature/Springer (nature.com, springer.com) — institutional access
    - MIT Press (direct.mit.edu) — Cloudflare bypass with browser cookies
    - J Neuroscience (jneurosci.org) — PDF link click
    - Elsevier (sciencedirect.com) — institutional PDF access
    - IEEE (ieeexplore.ieee.org) — institutional PDF access

Error handling:
    - Cloudflare challenge pages: wait + retry
    - Cookie consent / pop-ups: auto-dismiss
    - reCAPTCHA: pause and alert user
    - 404 / paywall: skip gracefully
    - Timeouts: configurable per-page
"""

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pipeline.browser")

# ─── Publisher Detection ────────────────────────────────────
PUBLISHER_PATTERNS = {
    "pnas": ["pnas.org"],
    "royal_society": ["royalsocietypublishing.org"],
    "nature_springer": ["nature.com", "springer.com", "springerlink.com"],
    "mit_press": ["direct.mit.edu", "mitpressjournals.org"],
    "jneurosci": ["jneurosci.org"],
    "elsevier": ["sciencedirect.com", "cell.com"],
    "ieee": ["ieeexplore.ieee.org"],
    "generic": [],  # fallback
}


def detect_publisher(url: str) -> str:
    """URL에서 publisher를 식별한다."""
    u = url.lower()
    for pub, domains in PUBLISHER_PATTERNS.items():
        for domain in domains:
            if domain in u:
                return pub
    return "generic"


# ─── Async Browser Download Engine ──────────────────────────
async def _download_single_doi(
    page,
    doi: str,
    download_dir: Path,
    timeout: int = 30000,
) -> tuple[bool, Optional[Path], str]:
    """
    단일 DOI에 대해 브라우저로 PDF를 다운로드한다.

    Flow:
    1. DOI → publisher 사이트로 리다이렉트
    2. Publisher 식별
    3. Publisher별 전략으로 PDF 다운로드
    4. Cloudflare challenge / pop-up 처리
    """
    safe_name = re.sub(r"[^\w\-.]", "_", doi) + ".pdf"
    dest = download_dir / safe_name

    # Skip if already downloaded
    if dest.exists() and dest.stat().st_size > 5000:
        return (True, dest, "cached")

    try:
        # Navigate to DOI
        response = await page.goto(
            f"https://doi.org/{doi}",
            wait_until="domcontentloaded",
            timeout=timeout,
        )

        # Wait for redirects to settle
        await page.wait_for_timeout(3000)

        # Handle Cloudflare challenge
        if await _handle_cloudflare(page):
            await page.wait_for_timeout(5000)

        # Dismiss cookie banners / pop-ups
        await _dismiss_popups(page)

        # Get final URL and detect publisher
        current_url = page.url
        publisher = detect_publisher(current_url)
        logger.info(f"  Publisher: {publisher} ({current_url[:60]})")

        # Publisher-specific download strategy
        strategy_map = {
            "pnas": _download_pnas,
            "royal_society": _download_royal_society,
            "nature_springer": _download_nature_springer,
            "mit_press": _download_generic_pdf_link,
            "jneurosci": _download_generic_pdf_link,
            "elsevier": _download_elsevier,
            "ieee": _download_ieee,
            "generic": _download_generic_pdf_link,
        }

        strategy = strategy_map.get(publisher, _download_generic_pdf_link)
        success = await strategy(page, doi, dest, timeout)

        if success and dest.exists() and dest.stat().st_size > 5000:
            return (True, dest, f"browser:{publisher}")
        else:
            if dest.exists():
                dest.unlink()
            return (False, None, "")

    except Exception as e:
        logger.error(f"  Browser download error: {e}")
        if dest.exists():
            dest.unlink()
        return (False, None, "")


# ─── Cloudflare / Pop-up Handling ───────────────────────────
async def _handle_cloudflare(page, max_wait: int = 15000) -> bool:
    """Cloudflare challenge page를 감지하고 대기한다."""
    content = await page.content()
    indicators = ["checking your browser", "cloudflare", "just a moment", "ddos-guard"]

    if any(ind in content.lower() for ind in indicators):
        logger.info("  ⏳ Cloudflare challenge detected, waiting...")
        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body.innerText.toLowerCase();
                    return !text.includes('checking') && !text.includes('just a moment');
                }""",
                timeout=max_wait,
            )
            return True
        except Exception:
            logger.warning("  ⚠ Cloudflare challenge timeout")
            return False
    return False


async def _dismiss_popups(page):
    """쿠키 배너, 팝업, 모달을 닫는다."""
    dismiss_selectors = [
        # Cookie consent
        'button:has-text("Accept")',
        'button:has-text("Accept All")',
        'button:has-text("Accept Cookies")',
        'button:has-text("Agree")',
        'button:has-text("OK")',
        'button:has-text("Got it")',
        'button:has-text("I Accept")',
        'button[id*="cookie"] >> text=Accept',
        'button[class*="cookie"] >> text=Accept',
        '[class*="consent"] button >> text=Accept',
        # Modal close
        'button[aria-label="Close"]',
        'button[class*="close-modal"]',
        '.modal-close',
    ]

    for selector in dismiss_selectors:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=500):
                await el.click()
                await page.wait_for_timeout(500)
                break
        except Exception:
            continue


async def _check_recaptcha(page) -> bool:
    """reCAPTCHA를 감지한다. 감지되면 True 반환하고 사용자 개입을 요청."""
    content = await page.content()
    if "recaptcha" in content.lower() or "g-recaptcha" in content.lower():
        logger.warning("  ⚠ reCAPTCHA detected! Manual intervention may be required.")
        logger.warning("    → Please solve the CAPTCHA in the browser window")
        # Wait up to 60 seconds for user to solve
        try:
            await page.wait_for_function(
                "() => !document.querySelector('.g-recaptcha, [class*=\"recaptcha\"]')",
                timeout=60000,
            )
            return True
        except Exception:
            return False
    return False


# ─── Publisher-Specific Strategies ──────────────────────────

async def _download_pnas(page, doi: str, dest: Path, timeout: int) -> bool:
    """PNAS: epdf viewer → JS fetch로 PDF blob 다운로드."""
    # Navigate to epdf viewer
    await page.goto(
        f"https://www.pnas.org/doi/epdf/{doi}",
        wait_until="domcontentloaded",
        timeout=timeout,
    )
    await page.wait_for_timeout(3000)
    await _handle_cloudflare(page)

    # Use JS fetch with browser cookies to download PDF
    pdf_bytes = await page.evaluate("""
        async (doi) => {
            try {
                const r = await fetch(`/doi/pdf/${doi}?download=true`);
                if (!r.ok) return null;
                const blob = await r.blob();
                const buf = await blob.arrayBuffer();
                return Array.from(new Uint8Array(buf));
            } catch(e) {
                return null;
            }
        }
    """, doi)

    if pdf_bytes and len(pdf_bytes) > 5000:
        dest.write_bytes(bytes(pdf_bytes))
        return _validate_pdf(dest)
    return False


async def _download_royal_society(page, doi: str, dest: Path, timeout: int) -> bool:
    """Royal Society: PDF 링크 클릭 → tokenized URL에서 다운로드."""
    # Find PDF link
    pdf_link = page.locator('a[href*="article-pdf"]').first
    try:
        href = await pdf_link.get_attribute("href", timeout=5000)
        if not href:
            return await _download_generic_pdf_link(page, doi, dest, timeout)
    except Exception:
        return await _download_generic_pdf_link(page, doi, dest, timeout)

    # Download via Playwright's download handler
    async with page.expect_download(timeout=timeout) as download_info:
        await pdf_link.click()
    download = await download_info.value
    await download.save_as(str(dest))
    return _validate_pdf(dest)


async def _download_nature_springer(page, doi: str, dest: Path, timeout: int) -> bool:
    """Nature/Springer: /content/pdf/ 패턴 또는 PDF 버튼."""
    # Try direct PDF URL pattern
    current_url = page.url
    pdf_url = current_url.replace("/article/", "/content/pdf/") + ".pdf"

    try:
        async with page.expect_download(timeout=timeout) as dl:
            await page.goto(pdf_url, wait_until="commit", timeout=timeout)
        download = await dl.value
        await download.save_as(str(dest))
        if _validate_pdf(dest):
            return True
    except Exception:
        pass

    # Fallback: find PDF link on page
    return await _download_generic_pdf_link(page, doi, dest, timeout)


async def _download_elsevier(page, doi: str, dest: Path, timeout: int) -> bool:
    """Elsevier/ScienceDirect: PDF 버튼 또는 /pdfft 엔드포인트."""
    await _dismiss_popups(page)

    # Find PII from URL
    pii_match = re.search(r"/pii/(\w+)", page.url)
    if pii_match:
        pdf_url = f"https://www.sciencedirect.com/science/article/pii/{pii_match.group(1)}/pdfft"
        pdf_bytes = await page.evaluate("""
            async (url) => {
                try {
                    const r = await fetch(url);
                    if (!r.ok) return null;
                    const blob = await r.blob();
                    const buf = await blob.arrayBuffer();
                    return Array.from(new Uint8Array(buf));
                } catch(e) { return null; }
            }
        """, pdf_url)

        if pdf_bytes and len(pdf_bytes) > 5000:
            dest.write_bytes(bytes(pdf_bytes))
            if _validate_pdf(dest):
                return True

    # Fallback: click PDF/Download button
    return await _download_generic_pdf_link(page, doi, dest, timeout)


async def _download_ieee(page, doi: str, dest: Path, timeout: int) -> bool:
    """IEEE Xplore: PDF 버튼 클릭."""
    await _dismiss_popups(page)

    # IEEE uses arnumber in URL
    arnumber = re.search(r"/document/(\d+)", page.url)
    if arnumber:
        pdf_url = f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnumber.group(1)}"
        try:
            async with page.expect_download(timeout=timeout) as dl:
                await page.goto(pdf_url, wait_until="commit", timeout=timeout)
            download = await dl.value
            await download.save_as(str(dest))
            if _validate_pdf(dest):
                return True
        except Exception:
            pass

    return await _download_generic_pdf_link(page, doi, dest, timeout)


async def _download_generic_pdf_link(page, doi: str, dest: Path, timeout: int) -> bool:
    """Generic: 페이지에서 PDF 링크를 찾아 다운로드."""
    # Strategy 1: Find links with 'pdf' in href
    pdf_selectors = [
        'a[href*="/pdf/"]',
        'a[href*=".pdf"]',
        'a[href*="pdf?"]',
        'a[href*="download=true"]',
        'a:has-text("PDF")',
        'a:has-text("Download PDF")',
        'a:has-text("Full Text (PDF)")',
        'button:has-text("PDF")',
    ]

    for selector in pdf_selectors:
        try:
            el = page.locator(selector).first
            if not await el.is_visible(timeout=2000):
                continue

            href = await el.get_attribute("href")
            if href:
                # If it's a relative URL, make it absolute
                if href.startswith("/"):
                    from urllib.parse import urlparse
                    parsed = urlparse(page.url)
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"

                # Try JS fetch first (handles CORS issues)
                pdf_bytes = await page.evaluate("""
                    async (url) => {
                        try {
                            const r = await fetch(url);
                            if (!r.ok) return null;
                            const ct = r.headers.get('content-type') || '';
                            if (ct.includes('html') && !ct.includes('pdf')) return null;
                            const blob = await r.blob();
                            const buf = await blob.arrayBuffer();
                            return Array.from(new Uint8Array(buf));
                        } catch(e) { return null; }
                    }
                """, href)

                if pdf_bytes and len(pdf_bytes) > 5000:
                    dest.write_bytes(bytes(pdf_bytes))
                    if _validate_pdf(dest):
                        return True

            # Try click + download handler
            try:
                async with page.expect_download(timeout=15000) as dl:
                    await el.click()
                download = await dl.value
                await download.save_as(str(dest))
                if _validate_pdf(dest):
                    return True
            except Exception:
                pass

        except Exception:
            continue

    # Strategy 2: citation_pdf_url meta tag
    try:
        pdf_url = await page.evaluate("""
            () => {
                const meta = document.querySelector('meta[name="citation_pdf_url"]');
                return meta ? meta.content : null;
            }
        """)
        if pdf_url:
            pdf_bytes = await page.evaluate("""
                async (url) => {
                    try {
                        const r = await fetch(url);
                        if (!r.ok) return null;
                        const blob = await r.blob();
                        const buf = await blob.arrayBuffer();
                        return Array.from(new Uint8Array(buf));
                    } catch(e) { return null; }
                }
            """, pdf_url)

            if pdf_bytes and len(pdf_bytes) > 5000:
                dest.write_bytes(bytes(pdf_bytes))
                if _validate_pdf(dest):
                    return True
    except Exception:
        pass

    return False


# ─── Validation ─────────────────────────────────────────────
def _validate_pdf(path: Path) -> bool:
    """PDF 파일인지 확인."""
    if not path.exists() or path.stat().st_size < 5000:
        return False
    with open(path, "rb") as f:
        return f.read(4) == b"%PDF"


# ─── Public API ─────────────────────────────────────────────
async def _run_browser_downloads(
    dois: list[str],
    download_dir: Path,
    headless: bool = False,
    timeout: int = 30000,
    delay: float = 2.0,
) -> list[dict]:
    """Playwright로 DOI 리스트의 PDF를 다운로드한다."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise ImportError("playwright not installed: pip install playwright && playwright install chromium")

    download_dir.mkdir(parents=True, exist_ok=True)
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        )
        page = await context.new_page()

        for i, doi in enumerate(dois, 1):
            logger.info(f"[{i}/{len(dois)}] Browser: {doi}")

            ok, path, source = await _download_single_doi(page, doi, download_dir, timeout)

            status = f"✅ {source}" if ok else "❌"
            size = f"{path.stat().st_size // 1024}KB" if ok and path else ""
            logger.info(f"  {status} {size}")

            results.append({
                "doi": doi,
                "success": ok,
                "path": str(path) if path else None,
                "source": source,
            })

            if i < len(dois):
                await asyncio.sleep(delay)

        await browser.close()

    return results


def browser_download_pdfs(
    dois: list[str],
    download_dir: Path | str,
    headless: bool = False,
    timeout: int = 30000,
    delay: float = 2.0,
) -> list[dict]:
    """
    Synchronous wrapper for browser-based PDF download.

    Args:
        dois: DOI strings to download
        download_dir: where to save PDFs
        headless: False = show browser (useful for CAPTCHA solving)
        timeout: per-page timeout in ms
        delay: seconds between downloads

    Returns:
        [{"doi": ..., "success": bool, "path": ..., "source": ...}]
    """
    download_dir = Path(download_dir)
    return asyncio.run(_run_browser_downloads(dois, download_dir, headless, timeout, delay))


# ─── CLI ────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Browser-based PDF download")
    parser.add_argument("--doi", "-d", help="Single DOI")
    parser.add_argument("--dois-file", help="File with DOIs (one per line)")
    parser.add_argument("--output", "-o", default="./pdfs", help="Output directory")
    parser.add_argument("--headless", action="store_true", help="Run headless (no browser window)")
    parser.add_argument("--timeout", type=int, default=30000, help="Per-page timeout (ms)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    dois = []
    if args.doi:
        dois = [args.doi]
    elif args.dois_file:
        with open(args.dois_file) as f:
            dois = [line.strip() for line in f if line.strip()]

    if not dois:
        print("No DOIs provided. Use --doi or --dois-file")
        exit(1)

    results = browser_download_pdfs(dois, args.output, args.headless, args.timeout)

    ok = sum(1 for r in results if r["success"])
    print(f"\nResult: {ok}/{len(results)} downloaded")
