import os
import re
import json
import time
import datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape as xml_escape
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
from google import genai
from auto_post_optimizer import PostOptimizer

# ================= 설정 구간 =================
SOURCE_URL = "https://www.healim.com/column_news"
TARGET_BRANCH_KEYWORDS = ["안양", "안양점", "안양지점", "유종민"]
SITE_DOMAIN = "https://hearimay.com"

COLUMN_DIR = "column"
SYNC_LOG_PATH = "column/synced_posts.json"
TEMPLATE_PATH = "column/template.html"
INDEX_PATH = "column/index.html"
SITEMAP_PATH = "sitemap.xml"
RSS_PATH = "rss.xml"

# Gemini API 클라이언트 (환경변수 GEMINI_API_KEY 사용)
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
optimizer = PostOptimizer()
_CACHED_CANDIDATE_MODELS = None
DEFAULT_MAX_OUTPUT_TOKENS = 8192
# ============================================

# 제외할 특정 모델명
EXCLUDED_EXACT_MODELS = {
    "gemini-2.5-flash",
}

# 텍스트/JSON 생성에 부적합한 특수 모달리티·전용 모델 제외 키워드
EXCLUDED_MODEL_KEYWORDS = (
    "image",
    "tts",
    "audio",
    "live",
    "embedding",
    "robotics",
    "computer-use",
    "customtools",
    "vision",
)

# 의료법 제56조(의료광고 심의) 준수 금지 표현 및 고위험 정규식 패턴
FORBIDDEN_MEDICAL_TERMS = (
    "한방신경정신과 전문의",
    "상담심리사 1급",
    "완치",
    "100% 치료",
    "100% 완치",
)

HIGH_RISK_MEDICAL_PATTERNS = (
    (r"부작용\s*없", "부작용 없음 단정 표현 금지"),
    (r"근본적(으로)?\s*(회복|해결|되찾|치료)", "근본적 회복/해결/치료 보장 표현 금지"),
    (r"(수면제|약물)\s*(감량|중단|없이|단약)", "수면제·약물 감량/중단/단약 유도 표현 금지"),
    (r"(편도체|기저핵)\s*안정", "특정 뇌 부위 기능 회복 단정 표현 금지"),
    (r"전두엽\s*활성", "전두엽 활성화 단정 표현 금지"),
    (r"밸런스\s*복원", "자율신경 밸런스 복원 보장 표현 금지"),
    (r"수면\s*스위치", "비의학적 과장 비유('수면 스위치') 금지"),
    (r"단일한?\s*병리", "의학적 과도한 단순화('단일 병리') 금지"),
    (r"직결된?다", "인과관계 단정 표현('직결된다') 금지"),
    (r"신경계\s*탈진", "비의학적 단정 표현('신경계 탈진') 금지"),
    (r"(확실히|확실한|충분히)\s*회복", "치료 결과 단정·보장 표현 금지"),
)

CHECKLIST_DISCLAIMER_HTML = (
    '<p class="checklist-disclaimer">'
    "※ 본 항목은 현재 상태를 살펴보기 위한 참고 사항이며, 질환을 진단하기 위한 검사가 아닙니다. "
    "증상이 지속되거나 일상생활에 지장이 있다면 의료진의 평가를 받아보시기 바랍니다."
    "</p>"
)

REQUIRED_EDITORIAL_COMPONENTS = (
    "summary-box",
    "callout-empathy",
    "mechanism-grid",
    "mechanism-card",
    "checklist-box",
    "doctor-note",
    "faq-item",
)


def _model_rank(model_name: str):
    """동일 계열 내에서 최신 안정 모델이 높은 우선순위를 갖도록 정렬 키 생성"""
    short = model_name.removeprefix("models/").lower()
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", short)
    major = int(m.group(1)) if m else 0
    minor = int(m.group(2)) if (m and m.group(2)) else 0

    is_latest_alias = 1 if short in ("gemini-pro-latest", "gemini-flash-latest") else 0
    not_lite = 0 if "lite" in short else 1
    if "exp" in short:
        stability = 0
    elif "preview" in short:
        stability = 1
    else:
        stability = 2
    is_canonical = 0 if re.search(r"-\d{2,4}", short) else 1

    return (major, minor, not_lite, stability, is_canonical, is_latest_alias, short)


def get_available_models():
    """현재 API 키로 generateContent가 가능한 모델을 조회해 Pro 우선 -> 최신 Flash 순으로 정렬"""
    global _CACHED_CANDIDATE_MODELS
    if _CACHED_CANDIDATE_MODELS is not None:
        return _CACHED_CANDIDATE_MODELS

    pro_candidates = []
    flash_candidates = []
    other_candidates = []

    for model in client.models.list():
        raw_name = getattr(model, "name", "") or ""
        short = raw_name.removeprefix("models/")
        short_lower = short.lower()

        # generateContent 지원 여부 확인 (google-genai SDK: supported_actions)
        actions = getattr(model, "supported_actions", None) or []
        if actions and "generateContent" not in actions:
            continue

        if not short_lower.startswith("gemini-"):
            continue
        if short_lower in EXCLUDED_EXACT_MODELS:
            continue
        if any(ex in short_lower for ex in EXCLUDED_MODEL_KEYWORDS):
            continue

        if "pro" in short_lower:
            pro_candidates.append(short)
        elif "flash" in short_lower:
            flash_candidates.append(short)
        else:
            other_candidates.append(short)

    pro_sorted = sorted(set(pro_candidates), key=_model_rank, reverse=True)
    flash_sorted = sorted(set(flash_candidates), key=_model_rank, reverse=True)
    candidates = pro_sorted + flash_sorted
    if not candidates:
        candidates = sorted(set(other_candidates), key=_model_rank, reverse=True)

    if not candidates:
        raise RuntimeError(
            "현재 GEMINI_API_KEY로 generateContent를 지원하는 Gemini 모델을 찾을 수 없습니다."
        )

    print(f"[모델 자동 조회] Pro 후보: {pro_sorted}")
    print(f"[모델 자동 조회] Flash 후보: {flash_sorted}")
    print(f"[모델 자동 선택] 최종 순회 우선순위: {candidates}")
    _CACHED_CANDIDATE_MODELS = candidates
    return _CACHED_CANDIDATE_MODELS


def _parse_json_response(raw_text: str) -> dict:
    """모델 응답 텍스트에서 JSON을 안전하게 파싱"""
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("모델 응답 JSON 최상위 객체가 dict 형태가 아닙니다.")
    return parsed


def call_gemini_json(prompt: str, step_label: str, max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS):
    """Pro 우선 -> 최신 Flash 순으로 순회 호출하며 404/429/503 등 오류 시 2초 대기 후 자동 폴백"""
    candidate_models = get_available_models()
    last_error = None

    for idx, model_name in enumerate(candidate_models):
        try:
            print(f"[{step_label}] 모델 호출 시도 ({idx + 1}/{len(candidate_models)}): {model_name}")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "max_output_tokens": max_output_tokens,
                },
            )
            data = _parse_json_response(response.text)
            print(f"[{step_label}] 모델 호출 성공 -> 최종 사용 모델: {model_name}")
            return data, model_name
        except Exception as e:
            last_error = e
            if idx + 1 < len(candidate_models):
                print(
                    f"[{step_label}] {model_name} 호출 실패 ({e}) "
                    f"-> 2초 대기 후 다음 순위 모델로 재시도합니다."
                )
                time.sleep(2)
            else:
                print(
                    f"[{step_label}] {model_name} 호출 실패 ({e}) "
                    f"-> 더 이상 시도할 후보 모델이 없습니다."
                )

    raise RuntimeError(f"[{step_label}] 사용 가능한 모든 Gemini 모델 호출에 실패했습니다: {last_error}")


def audit_editorial_quality(data: dict, report: dict) -> list:
    """auto_post_optimizer 결과 외에 의료법 제56조 정적 정규식·필수 HTML 컴포넌트·면책 문구·분량을 점검"""
    extra_issues = []
    combined_text = f"{data.get('title', '')}\n{data.get('meta_description', '')}\n{data.get('content_text', '')}\n{data.get('content_html', '')}"
    content_html = data.get("content_html", "")
    content_text = data.get("content_text", "")

    # 1) 단순 금지어 점검
    for term in FORBIDDEN_MEDICAL_TERMS:
        if term in combined_text:
            extra_issues.append(
                f"[의료광고법 위반] 금지 표현 '{term}' 감지 -> 반드시 제거하고 원장 약력은 '한방내과 전문의 · 상담심리학 박사 · 임상심리사 1급'으로 정확히 표기할 것."
            )

    # 2) 고위험 의료광고 정규식 패턴 점검
    for pattern, reason in HIGH_RISK_MEDICAL_PATTERNS:
        match = re.search(pattern, combined_text)
        if match:
            extra_issues.append(
                f"[의료광고법 고위험 정규식] '{match.group(0)}' 감지 ({reason}) -> 결과를 보장하거나 단정하지 않는 객관적·설명적 표현으로 교정할 것."
            )

    # 3) 필수 에디토리얼 HTML 컴포넌트 존재 여부 점검
    missing_components = [cls for cls in REQUIRED_EDITORIAL_COMPONENTS if cls not in content_html]
    if missing_components:
        extra_issues.append(
            f"필수 에디토리얼 HTML 컴포넌트 누락: {', '.join(missing_components)}"
        )

    # 4) .callout-empathy 내 1인칭 환자 대사(큰따옴표) 오인 소지 점검 (HTML 속성 제외, 텍스트만 검사)
    empathy_match = re.search(
        r'<div[^>]*class=["\'][^"\']*callout-empathy[^"\']*["\'][^>]*>(.*?)</div>',
        content_html,
        re.S | re.I,
    )
    if empathy_match:
        empathy_text = BeautifulSoup(empathy_match.group(1), "html.parser").get_text(" ", strip=True)
        if any(q in empathy_text for q in ('"', "“", "”")):
            extra_issues.append(
                "[의료광고법 위반 소지] .callout-empathy 내부에 큰따옴표 1인칭 환자 대사 포함 -> 치료경험담·후기 오인 방지를 위해 큰따옴표를 제거하고 객관적 3인칭 임상 양상 설명으로 작성할 것."
            )

    # 5) .checklist-box 내 비진단 면책 문구(.checklist-disclaimer) 필수 포함 점검
    if "checklist-box" in content_html:
        if "checklist-disclaimer" not in content_html or "진단하기 위한 검사가 아닙니다" not in content_html:
            extra_issues.append(
                f"[의료광고법 필수 누락] .checklist-box 하단에 비진단 면책 문구({CHECKLIST_DISCLAIMER_HTML})가 누락됨 -> 반드시 추가할 것."
            )

    # 6) .doctor-note 내부 중복 헤더 점검
    note_match = re.search(
        r'<div[^>]*class=["\'][^"\']*doctor-note[^"\']*["\'][^>]*>(.*?)</div>',
        content_html,
        re.S | re.I,
    )
    if note_match:
        note_inner = note_match.group(1)
        if re.search(r"<h[1-6]\b", note_inner, re.I) or "유종민 원장의 임상 노트" in note_inner:
            extra_issues.append(
                ".doctor-note 내부에 중복 헤더 제목이 포함됨 -> CSS ::before가 헤더를 자동 표시하므로 내부에는 본문 <p> 내용만 넣을 것."
            )

    # 7) 분량 점검
    if len(content_text.strip()) < 2600:
        extra_issues.append(
            f"본문 분량이 부족함(현재 {len(content_text.strip())}자) -> 공백 포함 3,000~4,000자 분량으로 의학적 설명과 임상 안내를 더 충실히 보강할 것."
        )

    return extra_issues


def review_medical_ad_compliance(data: dict) -> dict:
    """독립된 '엄격한 의료광고 심의 검토관(의료법 제56조)' 페르소나로 title/meta_description/content_html 전체를 사전 심의 및 직접 교정"""
    review_prompt = f"""
당신은 대한민국 보건복지부·대한한의사협회 기준을 적용하는 '엄격한 의료광고 심의 검토관(의료법 제56조 전문 심사위원)'입니다.
아래 제출된 한의원 칼럼 JSON(title, meta_description, content_text, content_html) 전체를 문장 단위로 감사(Audit)하고, 의료광고법 위반 소지나 과장·단정 표현이 있는 문장을 모두 안전한 의학적·설명적 문장으로 직접 교정(Rewrite)해 최종 JSON으로 반환하세요.

[6대 필수 심의 감사 항목]
1. 치료 효과 단정·보장 표현 여부:
   - '근본적 회복/해결/치료', '확실한/충분히 회복', '완치', '100% 치료' 같은 결과 보장 표현을 모두 제거하세요.
   - '편도체 안정', '전두엽 활성', '기저핵 안정', '교감·부교감 밸런스 복원', '수면 스위치가 켜진다' 등 특정 뇌 부위·신경의 기능 회복을 단정하는 표현을 금지하고, '증상 양상 평가 → 생리적·심리적 요인 탐색 → 한의학적 변증 → 개인별 치료 계획' 프레임의 설명적 문장으로 교정하세요.
2. '부작용 없음' 또는 타 치료법(양약·수면제·정신과 약물) 비교·비방 뉘앙스 여부:
   - '부작용 없이', '내성 없이' 등 안전성 단정 표현을 제거하세요.
   - 양약·수면제·정신과 약물의 부작용을 과도하게 부각해 한방 치료가 우월하다는 인상을 주거나 '수면제/약물 감량·중단·단약'을 유도하는 표현을 모두 교정하고, 기존 복용 약물은 임의로 중단하지 않고 처방 의료진과 상의해야 함을 명시하세요.
3. 의학적 단정·과도한 단순화 여부:
   - '단일한 병리', '직결된다', '신경계 탈진' 같은 단정적·비의학적 표현을 '~와 밀접하게 연관될 수 있다', '~가 관여할 수 있다' 등 엄밀한 의학적 표현으로 완화하세요.
   - HRV(심박변이도)·뇌파 검사가 단독 진단 도구처럼 서술되었다면, 문진 및 임상적 평가와 함께 해석하는 보조 검사임을 분명히 명시하세요.
4. 치료경험담·후기 오인 소지(1인칭 환자 인용문) 여부:
   - 특히 `.callout-empathy` 내부나 본문에 큰따옴표(" ")를 사용한 1인칭 환자 대사가 있다면 모두 제거하고, "몸은 피곤한데 잠자리에 누우면 오히려 생각이 많아지는 경우가 있습니다" 같은 객관적 3인칭 임상 양상 설명으로 교정하세요.
5. `.checklist-box` 내 비진단 면책 문구 누락 여부:
   - `.checklist-box` 하단에 아래 HTML 면책 문구가 정확히 포함되어 있는지 확인하고, 누락되거나 문구가 다르면 반드시 그대로 삽입하세요:
     {CHECKLIST_DISCLAIMER_HTML}
6. 한약·침·약침·바이오피드백 설명 방식 및 원장 약력 표기 정확성:
   - 각 치료법이 '효과 확정'이 아니라 '개인 상태에 따라 고려할 수 있는 치료 방법 소개'로 서술되도록 교정하세요.
   - 원장 약력은 '한방내과 전문의 · 상담심리학 박사 · 임상심리사 1급'으로 정확히 구분 표기하고, '한방신경정신과 전문의' 또는 '상담심리사 1급' 같은 오인 표현이 있다면 즉시 교정하세요.
   - 기존 HTML 구조(.summary-box, .callout-empathy, .mechanism-grid/.mechanism-card, .checklist-box, .doctor-note, <details class="faq-item">)와 .doctor-note 내부 중복 헤더 금지 원칙은 그대로 유지하고, content_text도 교정된 본문과 일치하도록 함께 업데이트하세요.

[출력 JSON 형식 (반드시 이 키 구조로만 반환)]
{{
  "fixed_count": 0,
  "modifications": [
    {{"rule": "위반 항목 번호/유형", "before": "수정 전 표현", "after": "수정 후 안전 표현"}}
  ],
  "title": "심의 교정 완료된 제목",
  "meta_description": "심의 교정 완료된 120자 이내 요약",
  "category": "{data.get('category', '')}",
  "slug": "{data.get('slug', '')}",
  "content_text": "심의 교정 완료된 순수 텍스트 본문 (문단 사이 \\n\\n 구분)",
  "content_html": "심의 교정 완료된 최종 HTML 본문"
}}

[심의 대상 원고 JSON]:
{json.dumps(data, ensure_ascii=False, indent=2)}
"""
    reviewed, model_name = call_gemini_json(
        review_prompt, step_label="Step 3: 의료광고법 사전 심의"
    )

    modifications = reviewed.get("modifications") or []
    fixed_count = reviewed.get("fixed_count")
    if not isinstance(fixed_count, int):
        fixed_count = len(modifications)

    print(
        f"[의료광고 심의 완료] 위험 표현 {fixed_count}건 교정 완료 "
        f"(심의 모델: {model_name})"
    )
    for i, mod in enumerate(modifications, 1):
        if isinstance(mod, dict):
            print(
                f"  - 교정 {i} [{mod.get('rule', '심의')}]: "
                f"'{mod.get('before', '')}' -> '{mod.get('after', '')}'"
            )
        else:
            print(f"  - 교정 {i}: {mod}")

    # 필수 키 보존
    result = {
        "title": reviewed.get("title") or data.get("title", ""),
        "meta_description": reviewed.get("meta_description") or data.get("meta_description", ""),
        "category": reviewed.get("category") or data.get("category", ""),
        "slug": reviewed.get("slug") or data.get("slug", ""),
        "content_text": reviewed.get("content_text") or data.get("content_text", ""),
        "content_html": reviewed.get("content_html") or data.get("content_html", ""),
    }
    return result


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
    """2단계 심층 에디토리얼 + 의료광고법 독립 심의 + 정적 정규식 감사 및 자동 재교정 파이프라인"""

    # ==================== STEP 1: 기획 & 아웃라인 설계 ====================
    step1_prompt = f"""
당신은 해아림한의원 안양점 유종민 대표원장(한방내과 전문의 · 상담심리학 박사 · 임상심리사 1급)의 의학 매거진 편집 기획자입니다.
아래 참고 원문의 핵심 질환과 임상 주제만 파악하고, 원문의 문장은 절대 복사하지 않은 채 완전히 새로운 6단계 심층 의학 칼럼 기획안(아웃라인)을 JSON으로 설계해 주세요.

[의료광고법(의료법 제56조) 핵심 준수 기준]
- 치료 방향은 반드시 '증상 양상 평가 → 생리적·심리적 요인 탐색 → 한의학적 변증 → 개인별 치료 계획' 구조로 기획하고, 결과 보장형 서술('근본적 회복/해결', '완치', '부작용 없이', '수면제/약물 감량·단약', '편도체 안정', '전두엽 활성', '기저핵 안정', '밸런스 복원', '수면 스위치')은 절대 포함하지 마세요.
- 원장 약력은 '한방내과 전문의 · 상담심리학 박사 · 임상심리사 1급'으로만 표기하고, '한방신경정신과 전문의' 또는 '상담심리사 1급'으로 오인될 표현은 금지합니다.
- 공감 박스는 치료경험담 오인을 막기 위해 큰따옴표 1인칭 환자 대사 대신 객관적 3인칭 임상 양상으로 기획하세요.
- HRV·뇌파 검사는 단독 진단 도구가 아니라 문진 및 임상적 평가와 함께 해석하는 보조 검사로 기획하세요.

[필수 6단계 목차 구조]
1. patient_pain_and_misconceptions: 환자의 실제 고통과 흔한 오해 (객관적 3인칭 임상 양상 설명 포함)
2. neuro_autonomic_mechanism: 자율신경·뇌신경 생리학적 기전 (단정 표현 배제, '~와 연관될 수 있다/~가 관여할 수 있다' 완화 서술)
3. psychological_connection: 심리·정서적 연결고리 (상담심리학 박사 · 임상심리사 1급 관점의 인지·정서·신체 상호작용)
4. self_checklist_criteria: 자가점검 기준 (진단 도구가 아닌 참고용 점검 항목 5~6개)
5. integrative_treatment_approach: 증상 양상 평가 → 생리적·심리적 요인 탐색 → 한의학적 변증 → 개인별 치료 계획 프레임으로 한약·침·약침·바이오피드백·심리상담 소개 (기존 복용 약물 임의 중단 금지 및 의료진 상의 안내 포함)
6. anyang_clinical_advice: 안양·평촌·범계 진료실에서의 현실적 임상 조언

[출력 JSON 형식]
{{
  "working_title": "25~40자 이내 핵심 질환 키워드 포함 가제목 (치료 보장·단정 표현 제외)",
  "category": "자율신경실조증 / 공황·불안장애 / 불면증 / 어지럼증 / 틱장애 / ADHD 중 가장 적합한 1개",
  "core_direct_answer": "AEO 두괄식 핵심 요약 2~3문장 ('결론적으로' 또는 '[핵심 요약]' 포함, 단정·과장 금지)",
  "target_entities": ["자율신경", "교감신경", "부교감신경", "HRV", "뇌파", "한의학 변증 등 6~8개"],
  "outline": {{
    "1_patient_pain_and_misconceptions": {{
      "heading": "섹션 소제목",
      "clinical_empathy_pattern": "큰따옴표 없는 객관적 3인칭 임상 양상 설명 (예: 몸은 피곤한데 잠자리에 누우면 오히려 생각이 많아지는 경우가 있습니다.)",
      "key_points": ["핵심 포인트 1", "핵심 포인트 2", "핵심 포인트 3"]
    }},
    "2_neuro_autonomic_mechanism": {{
      "heading": "섹션 소제목",
      "mechanism_cards": [
        {{"title": "기전 카드 1 제목", "detail": "생리학적 연관 메커니즘 요약 (단정 금지)"}},
        {{"title": "기전 카드 2 제목", "detail": "생리학적 연관 메커니즘 요약 (단정 금지)"}},
        {{"title": "기전 카드 3 제목", "detail": "심리·신경 상호작용 요약 (단정 금지)"}}
      ],
      "key_points": ["상세 설명 포인트 1", "상세 설명 포인트 2"]
    }},
    "3_psychological_connection": {{
      "heading": "섹션 소제목",
      "key_points": ["심리·정서 연결고리 포인트 1", "포인트 2", "포인트 3"]
    }},
    "4_self_checklist_criteria": {{
      "heading": "자가점검 체크리스트 제목 (참고용)",
      "items": ["점검 항목 1", "점검 항목 2", "점검 항목 3", "점검 항목 4", "점검 항목 5"]
    }},
    "5_integrative_treatment_approach": {{
      "heading": "섹션 소제목",
      "key_points": [
        "증상 양상 평가 및 보조 검사(HRV·뇌파는 문진·임상평가와 함께 해석하는 보조 검사)",
        "생리적·심리적 요인 탐색 및 한의학적 변증",
        "개인별 치료 계획(한약·침·약침·바이오피드백·심리상담 소개 및 기존 복용 약물 임의 중단 금지 안내)"
      ]
    }},
    "6_anyang_clinical_advice": {{
      "heading": "섹션 소제목",
      "doctor_note_angle": "유종민 원장의 임상 노트에 담을 따뜻하고 객관적인 임상 조언",
      "key_points": ["안양·평촌·범계 지역 환자를 위한 현실적 조언 1", "조언 2"]
    }}
  }},
  "faq_topics": [
    "환자가 가장 궁금해하는 질문 1?",
    "환자가 가장 궁금해하는 질문 2?",
    "환자가 가장 궁금해하는 질문 3?"
  ]
}}

[참고 원문 제목]: {orig_title}
[참고 원문 내용]:
{orig_content[:4000]}
"""
    outline_data, _ = call_gemini_json(step1_prompt, step_label="Step 1: 기획·아웃라인")

    # ==================== STEP 2: 심층 집필 & HTML 컴포넌트 조립 ====================
    editorial_writing_rules = f"""
[문체 및 분량 규칙]
1. 분량: 공백 포함 반드시 3,000~4,000자 분량으로 깊이 있고 충실하게 집필하세요.
2. 문체: 딱딱한 백과사전식 나열이나 광고성 문구를 완전히 배제하고, 진료실에서 환자와 1:1로 마주 앉아 차분하고 논리적으로 설명해 주는 '한방내과 전문의 · 상담심리학 박사 · 임상심리사 1급' 유종민 대표원장의 따뜻하고 엄밀한 에디토리얼 어조로 작성하세요.
3. 문단 가독성: 각 문단(<p>)은 모바일 가독성을 위해 2~3문장(180자 이내)으로 호흡을 나누어 작성하세요.
4. SEO/GEO/AEO 최적화:
   - 제목은 핵심 질환 키워드를 포함해 25~40자 내외로 작성하세요.
   - 서두 첫 부분(.summary-box)에 '[핵심 요약]' 또는 '결론적으로'로 시작하는 명확한 AEO 두괄식 요약을 배치하세요.
   - 자율신경, 교감신경, 부교감신경, HRV(심박변이도), 뇌파, 편도체, 전두엽, 기저핵, 한의학 변증, 기혈순환 등 해당 질환에 맞는 의학 엔티티 6개 이상을 엄밀하게 설명하세요.
   - 글 말미에는 안양점 1:1 정밀 평가·상담·네이버 예약 및 카카오톡 상담 안내(CTA)를 단정하게 포함하세요.

[의료광고법(의료법 제56조) 안전 원칙 — 절대 위반 금지]
1. 치료 방향 프레임:
   - '뇌신경 안정 → 복원 → 근본적 회복' 같은 결과 보장형 서술을 절대 금지합니다.
   - 반드시 '증상 양상 평가 → 생리적·심리적 요인 탐색 → 한의학적 변증 → 개인별 치료 계획' 구조로 서술하세요.
   - 한약·침·약침·바이오피드백은 '효과 확정'이 아니라 개인의 상태에 따라 적용을 고려할 수 있는 '치료 방법 소개'로 서술하세요.
2. 절대 금지 표현:
   - '근본적 회복/해결/치료', '부작용 없이', '수면제/약물 감량·중단·단약', '확실한/충분히 회복', '완치', '100% 치료' 금지.
   - 특정 뇌 부위나 신경의 기능 회복을 단정하는 표현('편도체 안정', '전두엽 활성', '기저핵 안정', '교감·부교감 밸런스 복원', '수면 스위치가 켜진다' 등) 금지.
3. 타 치료법 비교 금지:
   - 양약·수면제·정신과 약물의 부작용을 과도하게 강조해 한방 치료가 더 안전하거나 우월하다는 인상을 절대 주지 마세요.
   - 기존에 복용 중인 약물이 있다면 임의로 중단하지 않고 반드시 처방 의료진과 상의해야 함을 명시하세요.
4. 병리·검사 설명의 엄밀성:
   - '단일한 병리', '직결된다', '신경계 탈진' 같은 단정적·비의학적 표현을 금지하고, '~와 밀접하게 연관될 수 있다', '~가 관여할 수 있다'로 완화해 서술하세요.
   - HRV(심박변이도)·뇌파 검사는 단독 진단 도구가 아니라 문진 및 임상적 평가와 함께 해석하는 보조 검사임을 반드시 명시하세요.
5. 원장 약력 표기:
   - 반드시 '한방내과 전문의 · 상담심리학 박사 · 임상심리사 1급'으로 정확히 구분해 표기하세요.
   - '한방신경정신과 전문의' 또는 '상담심리사 1급'으로 오인될 표현은 절대 금지합니다.

[필수 HTML 컴포넌트 6종 (template.html CSS와 100% 일치하게 모두 포함)]
1. <div class="summary-box"><p><strong>[핵심 요약]</strong> ...</p></div> (글 최상단 배치, 결과 단정 금지)
2. <div class="callout-empathy"><p>...</p></div>
   ★ 중요: 실제 환자의 치료경험담·후기로 오인되지 않도록 큰따옴표(" ")를 사용한 1인칭 환자 대사 형식을 절대 금지합니다. 반드시 "몸은 피곤한데 잠자리에 누우면 오히려 생각이 많아지는 경우가 있습니다"와 같은 객관적 3인칭 임상 양상 설명으로 작성하세요.
3. <div class="mechanism-grid"><div class="mechanism-card"><h3>...</h3><p>...</p></div> ... </div> (2~3개 카드로 자율신경·뇌신경·심리 연관 기전 정리)
4. <div class="checklist-box"><h3>...</h3><ul><li>...</li>...</ul>{CHECKLIST_DISCLAIMER_HTML}</div>
   ★ 중요: .checklist-box 하단에는 반드시 위 `<p class="checklist-disclaimer">...</p>` 면책 문구를 토씨 하나 바꾸지 말고 그대로 포함하세요.
5. <div class="doctor-note"><p>...</p></div>
   ★ 중요: .doctor-note 내부에는 CSS(::before)가 '유종민 원장의 임상 노트 · 한방내과 전문의 · 상담심리학 박사' 헤더를 자동으로 그려줍니다. 따라서 .doctor-note 내부에는 절대로 <h3>, <h4> 같은 헤더 제목이나 '유종민 원장의 임상 노트' 문구를 중복 작성하지 말고, 순수 임상 노트 본문 <p> 문단만 넣으세요.
6. <details class="faq-item"><summary>Q. ...</summary><p>A. ...</p></details> (자주 묻는 질문 3개를 각각 <details class="faq-item"> 아코디언으로 작성)

[출력 JSON 형식]
{{
  "title": "25~40자 이내의 핵심 질환 키워드 포함 제목",
  "meta_description": "120자 이내 검색 결과용 핵심 요약",
  "category": "자율신경실조증 / 공황·불안장애 / 불면증 / 어지럼증 / 틱장애 / ADHD 중 택1",
  "slug": "영문-소문자-하이픈-슬러그",
  "content_text": "content_html의 전체 본문을 순수 텍스트로 정리한 버전 (문단 사이 빈 줄 2개 \\n\\n 구분, AEO 요약·Q&A·CTA 포함, 공백 포함 3,000~4,000자)",
  "content_html": "위 6가지 CSS 컴포넌트와 <h2>, <h3>, <p>, <ul> 태그로 구성된 완성된 에디토리얼 HTML 본문"
}}
"""

    step2_prompt = f"""
당신은 한방내과 전문의 · 상담심리학 박사 · 임상심리사 1급인 해아림한의원 안양점 유종민 대표원장입니다.
아래 Step 1 기획안(아웃라인)을 바탕으로 고급 의학 매거진(에디토리얼) 수준의 심층 칼럼을 집필해 주세요.
참고 원문의 문장을 복사하지 말고, 기획안의 6단계 흐름과 의료광고법 안전 원칙을 철저히 지켜 완성하세요.

{editorial_writing_rules}

[Step 1 기획안 JSON]:
{json.dumps(outline_data, ensure_ascii=False, indent=2)}
"""
    data, _ = call_gemini_json(step2_prompt, step_label="Step 2: 심층 집필·HTML 조립")

    # ==================== STEP 3: 독립 의료광고법(제56조) 사전 심의 게이트 ====================
    data = review_medical_ad_compliance(data)

    # ==================== STEP 4: 품질 점수 + 정적 정규식 감사 및 자동 재교정 ====================
    report = optimizer.analyze_post(data["title"], data["content_text"])
    extra_issues = audit_editorial_quality(data, report)
    all_issues = report["issues"] + extra_issues
    print(
        f"[심의 후 1차 감사 결과] {data['title']} -> {report['score']}점 / "
        f"분량: {len(data['content_text'].strip())}자 / 잔여 이슈({len(all_issues)}건): {all_issues}"
    )

    if report["score"] < 80 or all_issues:
        print(
            "[경고 및 자동 재교정 시작] 심의 후에도 80점 미만 또는 의료광고법/품질 이슈가 남아 있어 "
            "1회 자동 재교정(Refine)을 실행합니다."
        )
        refine_prompt = f"""
당신은 한방내과 전문의 · 상담심리학 박사 · 임상심리사 1급인 해아림한의원 안양점 유종민 대표원장이자 의료광고법(의료법 제56조) 최종 검수자입니다.
아래 사전 심의를 거친 칼럼 JSON을 정적 정규식 및 품질 검증기로 재감사한 결과, 다음 항목들이 여전히 남아 있거나 보완이 필요합니다.
기존 글의 차분한 진료실 문체와 HTML 구조를 유지하면서, 지적된 모든 의료광고법 위반 소지와 품질 이슈를 100% 해소한 최종 JSON을 반환해 주세요.

[현재 검증 점수]: {report['score']} / {report['max_score']}점
[반드시 해결해야 할 경고·이슈]:
{json.dumps(all_issues, ensure_ascii=False, indent=2)}
[개선 권장 사항]:
{json.dumps(report['improvements'], ensure_ascii=False, indent=2)}

{editorial_writing_rules}

[재교정 대상 칼럼 JSON]:
{json.dumps(data, ensure_ascii=False, indent=2)}
"""
        refined_data, _ = call_gemini_json(refine_prompt, step_label="Step 4: 의료법·품질 자동 재교정")
        refined_report = optimizer.analyze_post(refined_data["title"], refined_data["content_text"])
        refined_extra = audit_editorial_quality(refined_data, refined_report)
        remaining_issues = refined_report["issues"] + refined_extra
        print(
            f"[재교정 후 최종 검증] {refined_data['title']} -> "
            f"{report['score']}점 -> {refined_report['score']}점 / "
            f"분량: {len(refined_data['content_text'].strip())}자 / "
            f"잔여 이슈({len(remaining_issues)}건): {remaining_issues}"
        )
        if refined_extra:
            print(f"[최종 경고] 재교정 후에도 정적 감사 항목이 남아 있습니다: {refined_extra}")
        data, report = refined_data, refined_report

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
      <div class="card-footer">
        <time>{today}</time>
        <a href="/{rel_path}" class="card-more">자세히 보기 →</a>
      </div>
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


def generate_rss_feed(max_items: int = 20):
    """column/ 폴더 내 개별 칼럼 HTML을 스캔해 네이버 서치어드바이저용 표준 RSS 2.0(rss.xml) 생성·갱신"""
    kst = datetime.timezone(datetime.timedelta(hours=9))
    excluded_files = {"index.html", "template.html"}
    items = []

    if os.path.isdir(COLUMN_DIR):
        for filename in os.listdir(COLUMN_DIR):
            if not filename.endswith(".html") or filename in excluded_files:
                continue

            filepath = os.path.join(COLUMN_DIR, filename)
            if not os.path.isfile(filepath):
                continue

            with open(filepath, "r", encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), "html.parser")

            # 제목: <h1> 우선, 없으면 <title>에서 사이트 접미사 제거
            h1_el = soup.select_one("main h1") or soup.select_one("h1")
            if h1_el and h1_el.get_text(" ", strip=True):
                title = h1_el.get_text(" ", strip=True)
            elif soup.title and soup.title.get_text(" ", strip=True):
                title = soup.title.get_text(" ", strip=True).split("|")[0].strip()
            else:
                title = os.path.splitext(filename)[0]

            # 요약: meta description 우선, 없으면 og:description / 첫 문단
            meta_desc = soup.find("meta", attrs={"name": "description"})
            og_desc = soup.find("meta", attrs={"property": "og:description"})
            if meta_desc and meta_desc.get("content"):
                description = meta_desc["content"].strip()
            elif og_desc and og_desc.get("content"):
                description = og_desc["content"].strip()
            else:
                first_p = soup.select_one(".column-content p")
                description = first_p.get_text(" ", strip=True)[:160] if first_p else title

            # 카테고리: Breadcrumb 마지막 span 또는 tag 아이콘 부모 span
            category = "치료칼럼"
            breadcrumb_cat = soup.select_one('nav[aria-label="Breadcrumb"] span')
            if breadcrumb_cat and breadcrumb_cat.get_text(" ", strip=True):
                category = breadcrumb_cat.get_text(" ", strip=True)
            else:
                tag_icon = soup.select_one('i[data-lucide="tag"]')
                if tag_icon and tag_icon.parent:
                    cat_text = tag_icon.parent.get_text(" ", strip=True)
                    if cat_text:
                        category = cat_text

            # 날짜: 파일명 YYYY-MM-DD 기준 (없으면 <time> 또는 파일 수정 시각)
            date_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})-", filename)
            if date_match:
                y, m, d = map(int, date_match.groups())
                pub_dt = datetime.datetime(y, m, d, 9, 0, 0, tzinfo=kst)
            else:
                time_el = soup.select_one("time")
                time_match = (
                    re.search(r"(\d{4})-(\d{2})-(\d{2})", time_el.get_text(strip=True))
                    if time_el
                    else None
                )
                if time_match:
                    y, m, d = map(int, time_match.groups())
                    pub_dt = datetime.datetime(y, m, d, 9, 0, 0, tzinfo=kst)
                else:
                    pub_dt = datetime.datetime.fromtimestamp(os.path.getmtime(filepath), tz=kst)

            url = f"{SITE_DOMAIN}/column/{filename}"
            items.append(
                {
                    "title": title,
                    "description": description,
                    "category": category,
                    "pub_dt": pub_dt,
                    "pub_date_rfc822": format_datetime(pub_dt),
                    "url": url,
                    "filename": filename,
                }
            )

    # 최신 날짜순 정렬 후 최대 max_items(20)개 선택
    items.sort(key=lambda x: (x["pub_dt"], x["filename"]), reverse=True)
    recent_items = items[:max_items]

    last_build_date = format_datetime(datetime.datetime.now(kst))
    rss_url = f"{SITE_DOMAIN}/rss.xml"
    channel_link = f"{SITE_DOMAIN}/column/"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{xml_escape('해아림한의원 안양점 치료칼럼')}</title>",
        f"    <link>{xml_escape(channel_link)}</link>",
        f"    <description>{xml_escape('한방내과 전문의 · 상담심리학 박사 유종민 원장이 전하는 자율신경실조증, 공황장애, 불면증, 틱장애, ADHD 심층 의학 칼럼')}</description>",
        "    <language>ko</language>",
        f"    <lastBuildDate>{last_build_date}</lastBuildDate>",
        f'    <atom:link href="{xml_escape(rss_url)}" rel="self" type="application/rss+xml" />',
    ]

    for item in recent_items:
        lines.extend(
            [
                "    <item>",
                f"      <title>{xml_escape(item['title'])}</title>",
                f"      <link>{xml_escape(item['url'])}</link>",
                f'      <guid isPermaLink="true">{xml_escape(item["url"])}</guid>',
                f"      <description>{xml_escape(item['description'])}</description>",
                f"      <category>{xml_escape(item['category'])}</category>",
                f"      <pubDate>{item['pub_date_rfc822']}</pubDate>",
                "    </item>",
            ]
        )

    lines.extend(["  </channel>", "</rss>", ""])

    with open(RSS_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[RSS 갱신 완료] {RSS_PATH} (총 {len(recent_items)}개 항목 반영)")


def main():
    synced_urls = load_synced_urls()
    new_posts = fetch_new_anyang_posts(synced_urls)

    if not new_posts:
        print("신규 안양지점 칼럼이 없습니다.")
        if not os.path.exists(RSS_PATH):
            generate_rss_feed()
        return

    created_count = 0
    for post in new_posts:
        try:
            orig_content = fetch_post_content(post["url"])
            upgraded_data, report = rewrite_and_upgrade_column(post["title"], orig_content)
            build_column_page(upgraded_data, report)
            synced_urls.append(post["url"])
            created_count += 1
        except Exception as e:
            print(f"[오류] {post['url']}: {e}")

    save_synced_urls(synced_urls)
    if created_count > 0 or not os.path.exists(RSS_PATH):
        generate_rss_feed()


if __name__ == "__main__":
    main()