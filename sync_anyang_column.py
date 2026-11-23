import os
import re
import json
import datetime
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
from google import genai
from auto_post_optimizer import PostOptimizer

# ================= 설정 구간 =================
SOURCE_URL = "https://www.healim.com/column_news"
TARGET_BRANCH_KEYWORDS = ["안양", "안양점", "안양지점", "유종민"]
SITE_DOMAIN = "https://hearimay.com"

SYNC_LOG_PATH = "column/synced_posts.json"
TEMPLATE_PATH = "column/template.html"
INDEX_PATH = "column/index.html"
SITEMAP_PATH = "sitemap.xml"

# Gemini API 클라이언트 (환경변수 GEMINI_API_KEY 사용)
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
optimizer = PostOptimizer()
# ============================================


def load_synced_urls():
    if not os.path.exists(SYNC_LOG_PATH):
        return []
    with open(SYNC_LOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_synced_urls(urls):
    with open(SYNC_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(urls, f, ensure_ascii=False, indent=2)


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def normalize_post_url(url):
    """KBoard 게시글 URL에서 변동 파라미터(pageid)를 제거해 uid 기준 고유 URL로 정규화"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs.pop("pageid", None)
    normalized_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=normalized_query))


def fetch_new_anyang_posts(synced_urls):
    """healim.com/column_news (WordPress KBoard)에서 안양지점 신규 글 목록 추출"""
    res = requests.get(SOURCE_URL, headers=REQUEST_HEADERS, timeout=15)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    normalized_synced = {normalize_post_url(u) for u in synced_urls}
    seen_urls = set()
    new_posts = []

    # KBoard 게시판 목록 행: #kboard-default-list .kboard-list tbody tr
    rows = soup.select("#kboard-default-list .kboard-list tbody tr") or soup.select(".kboard-list tbody tr")
    for row in rows:
        a_tag = row.select_one("td.kboard-list-title > a[href]") or row.select_one("td.kboard-list-title a[href]")
        if not a_tag:
            continue

        href = a_tag.get("href", "")
        if "mod=document" not in href:
            continue

        # 작성자(지점명): td.kboard-list-user (예: '안양지점')
        user_el = row.select_one("td.kboard-list-user")
        writer = user_el.get_text(" ", strip=True) if user_el else ""
        if not writer:
            mobile_user = row.select_one("td.kboard-list-title .kboard-mobile-contents .contents-item")
            writer = mobile_user.get_text(" ", strip=True) if mobile_user else ""

        # 제목 추출 시 KBoard 'New' 배지 및 댓글 수 span 제외
        title_el = a_tag.select_one(".kboard-default-cut-strings") or a_tag
        for badge in title_el.select(".kboard-default-new-notify, .kboard-comments-count"):
            badge.decompose()
        title = title_el.get_text(" ", strip=True)

        # 작성자(지점) 기준 안양점 게시글 필터링 (작성자 미표기 시 제목 보조 확인)
        match_target = writer if writer else title
        if not any(k in match_target for k in TARGET_BRANCH_KEYWORDS):
            continue

        full_url = normalize_post_url(urljoin(SOURCE_URL, href))
        if full_url not in normalized_synced and full_url not in seen_urls:
            seen_urls.add(full_url)
            new_posts.append({"title": title, "url": full_url})

    return new_posts


def fetch_post_content(url):
    """healim.com/column_news KBoard 상세 페이지에서 순수 본문 텍스트 추출"""
    res = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    # KBoard 상세 본문 영역: #kboard-default-document .kboard-content .content-view
    body = (
        soup.select_one("#kboard-default-document .kboard-content .content-view")
        or soup.select_one(".kboard-document-wrap .kboard-content .content-view")
        or soup.select_one(".kboard-content .content-view")
        or soup.select_one(".kboard-content")
        or soup.body
    )
    for tag in body.select("script, style"):
        tag.decompose()
    return body.get_text("\n", strip=True)


def rewrite_and_upgrade_column(orig_title, orig_content):
    """본원 글과 중복되지 않게 차별화 + 의학적 깊이/AEO/GEO 강화"""
    prompt = f"""
당신은 한방내과 전문의이자 상담심리학 박사인 해아림한의원 안양점 유종민 원장입니다.
아래 본원 칼럼 원문을 참고하되, 검색엔진 중복 문서 페널티를 피하기 위해 문장 구조와 전개 방식을 완전히 새롭게 재구성하고 내용을 한 단계 업그레이드해 주세요.

[필수 조건]
1. 원문 문장을 그대로 복사하지 말 것 (완전한 재서술 및 심화).
2. 자율신경계, 뇌신경 생리학, 심리적 기전 등 현대 의학적 설명을 자연스럽게 보강할 것.
3. 안양·평촌·범계 지역 환자들이 공감할 수 있는 임상적 조언을 포함할 것.
4. 서두에 핵심 질문에 대한 두괄식 요약(AEO 대응)을 넣고, 하단에 '자주 묻는 질문(Q&A)' 3개를 추가할 것.
5. 출력은 반드시 아래 JSON 형식으로만 반환할 것:
{{
  "title": "20~45자 이내의 핵심 질환 키워드 포함 제목",
  "meta_description": "120자 이내 요약",
  "category": "자율신경 / 공황·불안 / 불면증 / 틱·ADHD 중 택1",
  "slug": "영문-소문자-하이픈-슬러그",
  "content_text": "순수 텍스트 본문 (검증용)",
  "content_html": "<h2>, <p>, <ul> 등 시맨틱 HTML 태그로 구성된 본문"
}}

[원문 제목]: {orig_title}
[원문 내용]:
{orig_content[:4000]}
"""
    response = client.models.generate_content(
        model="gemini-3.0-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    data = json.loads(response.text)

    # 기존 auto_post_optimizer.py로 품질 검증
    report = optimizer.analyze_post(data["title"], data["content_text"])
    print(f"[검증 점수] {data['title']} -> {report['score']}점 / 이슈: {report['issues']}")
    return data, report


def build_column_page(data, report):
    today = datetime.date.today().isoformat()
    filename = f"{today}-{re.sub(r'[^a-z0-9-]', '', data['slug'].lower())}.html"
    rel_path = f"column/{filename}"
    canonical_url = f"{SITE_DOMAIN}/{rel_path}"

    schema_obj = report.get("schema_ldjson") or {
        "@context": "https://schema.org",
        "@type": "MedicalWebPage",
        "headline": data["title"],
        "datePublished": today,
        "author": {"@type": "Person", "name": "유종민 원장"},
        "url": canonical_url,
    }

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        tpl = f.read()

    html = (
        tpl.replace("{{TITLE}}", data["title"])
        .replace("{{META_DESCRIPTION}}", data["meta_description"])
        .replace("{{CANONICAL_URL}}", canonical_url)
        .replace("{{DATE}}", today)
        .replace("{{CATEGORY}}", data["category"])
        .replace("{{CONTENT_HTML}}", data["content_html"])
        .replace("{{SCHEMA_LDJSON}}", json.dumps(schema_obj, ensure_ascii=False))
    )

    with open(rel_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 1) column/index.html 목록 상단에 새 카드 삽입
    card_html = f"""
    <article class="column-card">
      <span class="category">{data['category']}</span>
      <h3><a href="/{rel_path}">{data['title']}</a></h3>
      <p>{data['meta_description']}</p>
      <time>{today}</time>
    </article>
"""
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        idx_html = f.read()

    idx_html = idx_html.replace(
        "<!-- COLUMN_LIST_START -->",
        f"<!-- COLUMN_LIST_START -->\n{card_html}",
        1,
    )
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(idx_html)

    # 2) sitemap.xml에 신규 URL 추가
    with open(SITEMAP_PATH, "r", encoding="utf-8") as f:
        sitemap = f.read()

    if canonical_url not in sitemap:
        new_entry = f"""  <url>
    <loc>{canonical_url}</loc>
    <lastmod>{today}</lastmod>
    <priority>0.8</priority>
  </url>
</urlset>"""
        sitemap = sitemap.replace("</urlset>", new_entry)
        with open(SITEMAP_PATH, "w", encoding="utf-8") as f:
            f.write(sitemap)

    print(f"[생성 완료] {canonical_url}")


def main():
    synced_urls = load_synced_urls()
    new_posts = fetch_new_anyang_posts(synced_urls)

    if not new_posts:
        print("신규 안양지점 칼럼이 없습니다.")
        return

    for post in new_posts:
        try:
            orig_content = fetch_post_content(post["url"])
            upgraded_data, report = rewrite_and_upgrade_column(post["title"], orig_content)
            build_column_page(upgraded_data, report)
            synced_urls.append(post["url"])
        except Exception as e:
            print(f"[오류] {post['url']}: {e}")

    save_synced_urls(synced_urls)


if __name__ == "__main__":
    main()