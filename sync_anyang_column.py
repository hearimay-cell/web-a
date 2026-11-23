import os
import re
import json
import time
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

# 의료법 준수 금지 표현 및 필수 에디토리얼 HTML 컴포넌트
FORBIDDEN_MEDICAL_TERMS = (
    "한방신경정신과 전문의",
    "완치",
    "100% 치료",
    "100% 완치",
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
    """auto_post_optimizer 결과 외에 의료법 가드레일·필수 HTML 컴포넌트·분량을 추가 점검"""
    extra_issues = []
    combined_text = f"{data.get('title', '')}\n{data.get('content_text', '')}\n{data.get('content_html', '')}"
    content_html = data.get("content_html", "")
    content_text = data.get("content_text", "")

    for term in FORBIDDEN_MEDICAL_TERMS:
        if term in combined_text:
            extra_issues.append(
                f"의료법 준수 위반 표현 '{term}' 포함 -> 반드시 제거하고 전문의 표기는 '한방내과 전문의 · 상담심리학 박사'로만 작성할 것."
            )

    missing_components = [cls for cls in REQUIRED_EDITORIAL_COMPONENTS if cls not in content_html]
    if missing_components:
        extra_issues.append(
            f"필수 에디토리얼 HTML 컴포넌트 누락: {', '.join(missing_components)}"
        )

    # .doctor-note 내부 중복 헤더 점검
    note_match = re.search(r'<div[^>]*class=["\'][^"\']*doctor-note[^"\']*["\'][^>]*>(.*?)</div>', content_html, re.S | re.I)
    if note_match:
        note_inner = note_match.group(1)
        if re.search(r"<h[1-6]\b", note_inner, re.I) or "유종민 원장의 임상 노트" in note_inner:
            extra_issues.append(
                ".doctor-note 내부에 중복 헤더 제목이 포함됨 -> CSS ::before가 헤더를 자동 표시하므로 내부에는 본문 <p> 내용만 넣을 것."
            )

    if len(content_text.strip()) < 2600:
        extra_issues.append(
            f"본문 분량이 부족함(현재 {len(content_text.strip())}자) -> 공백 포함 3,000~4,000자 분량으로 의학적 설명과 임상 사례를 더 충실히 보강할 것."
        )

    return extra_issues


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
    """2단계 심층 에디토리얼 파이프라인 (Step 1 기획 -> Step 2 심층 집필 -> 검증 및 1회 자동 보완)"""

    # ==================== STEP 1: 기획 & 아웃라인 설계 ====================
    step1_prompt = f"""
당신은 해아림한의원 안양점 유종민 대표원장(한방내과 전문의 · 상담심리학 박사)의 의학 매거진 편집 기획자입니다.
아래 참고 원문의 핵심 질환과 임상 주제만 파악하고, 원문의 문장은 절대 복사하지 않은 채 완전히 새로운 6단계 심층 의학 칼럼 기획안(아웃라인)을 JSON으로 설계해 주세요.

[의료법 준수 가드레일]
- '한방신경정신과 전문의'라는 표현은 절대 사용하지 말고, 반드시 '한방내과 전문의 · 상담심리학 박사'로만 표기하세요.
- '완치', '100% 치료', '100% 완치' 같은 과장 표현은 절대 사용하지 마세요.

[필수 6단계 목차 구조]
1. patient_pain_and_misconceptions: 환자의 실제 고통과 흔한 오해 (진료실에서 환자가 자주 호소하는 생생한 표현 포함)
2. neuro_autonomic_mechanism: 자율신경·뇌신경 생리학적 기전 (교감/부교감신경, HRV, 편도체, 전두엽, HPA축 등 의학 엔티티 반영)
3. psychological_connection: 심리·정서적 연결고리 (상담심리학 박사 관점의 인지·정서·신체 상호작용)
4. self_checklist_criteria: 자가점검 기준 (환자가 스스로 점검해 볼 수 있는 구체적 증상 5~6개)
5. integrative_treatment_approach: 한의학적·통합적 치료 접근 (변증, 체질 맞춤 한약, 침·약침, 두뇌 훈련, 심리 상담의 균형적 설명)
6. anyang_clinical_advice: 안양·평촌·범계 진료실에서의 임상 조언 (지역 환자·학부모·직장인이 공감할 현실적 회복 가이드)

[출력 JSON 형식]
{{
  "working_title": "25~40자 이내 핵심 질환 키워드 포함 가제목",
  "category": "자율신경실조증 / 공황·불안장애 / 불면증 / 어지럼증 / 틱장애 / ADHD 중 가장 적합한 1개",
  "core_direct_answer": "AEO 두괄식 핵심 요약 2~3문장 ('결론적으로' 또는 '[핵심 요약]' 포함)",
  "target_entities": ["자율신경", "교감신경", "부교감신경", "HRV", "뇌파", "한의학 변증 등 6~8개"],
  "outline": {{
    "1_patient_pain_and_misconceptions": {{
      "heading": "섹션 소제목",
      "patient_quote": "진료실에서 환자가 자주 하는 공감 인용 문장",
      "key_points": ["핵심 포인트 1", "핵심 포인트 2", "핵심 포인트 3"]
    }},
    "2_neuro_autonomic_mechanism": {{
      "heading": "섹션 소제목",
      "mechanism_cards": [
        {{"title": "기전 카드 1 제목", "detail": "생리학적 메커니즘 요약"}},
        {{"title": "기전 카드 2 제목", "detail": "생리학적 메커니즘 요약"}},
        {{"title": "기전 카드 3 제목", "detail": "심리·신경 연결 메커니즘 요약"}}
      ],
      "key_points": ["상세 설명 포인트 1", "상세 설명 포인트 2"]
    }},
    "3_psychological_connection": {{
      "heading": "섹션 소제목",
      "key_points": ["심리·정서 연결고리 포인트 1", "포인트 2", "포인트 3"]
    }},
    "4_self_checklist_criteria": {{
      "heading": "자가점검 체크리스트 제목",
      "items": ["점검 항목 1", "점검 항목 2", "점검 항목 3", "점검 항목 4", "점검 항목 5"]
    }},
    "5_integrative_treatment_approach": {{
      "heading": "섹션 소제목",
      "key_points": ["정량 검사(HRV/뇌파 등)", "한의학 변증 및 맞춤 한약·침구", "심리적 조력 및 생활 관리"]
    }},
    "6_anyang_clinical_advice": {{
      "heading": "섹션 소제목",
      "doctor_note_angle": "유종민 원장의 임상 노트에 담을 따뜻하고 깊이 있는 통찰",
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
    editorial_writing_rules = """
[문체 및 분량 규칙]
1. 분량: 공백 포함 반드시 3,000~4,000자 분량으로 깊이 있고 충실하게 집필하세요.
2. 문체: 딱딱한 백과사전식 나열이나 광고성 문구를 완전히 배제하고, 진료실에서 환자와 1:1로 마주 앉아 차분하고 논리적으로 설명해 주는 '한방내과 전문의 · 상담심리학 박사' 유종민 대표원장의 따뜻하고 신뢰감 있는 에디토리얼 어조로 작성하세요.
3. 문단 가독성: 각 문단(<p>)은 모바일 가독성을 위해 2~3문장(180자 이내)으로 호흡을 나누어 작성하세요.
4. SEO/GEO/AEO 최적화:
   - 제목은 핵심 질환 키워드를 포함해 25~40자 내외로 작성하세요.
   - 서두 첫 부분(.summary-box)에 '[핵심 요약]' 또는 '결론적으로'로 시작하는 명확한 AEO 두괄식 요약을 배치하세요.
   - 자율신경, 교감신경, 부교감신경, HRV(심박변이도), 뇌파, 편도체, 전두엽, 기저핵, 한의학 변증, 기혈순환 등 해당 질환에 맞는 의학 엔티티 6개 이상과 정량 검사 기준을 자연스럽게 녹여내세요.
   - 글 말미에는 안양점 1:1 정밀 검사·상담·네이버 예약 및 카카오톡 상담 안내(CTA)를 단정하게 포함하세요.

[필수 HTML 컴포넌트 6종 (template.html CSS와 100% 일치하게 모두 포함)]
1. <div class="summary-box"><p><strong>[핵심 요약]</strong> ...</p></div> (글 최상단 배치)
2. <div class="callout-empathy"><p>"..."</p></div> (진료실 환자 공감 인용)
3. <div class="mechanism-grid"><div class="mechanism-card"><h3>...</h3><p>...</p></div> ... </div> (2~3개 카드로 자율신경·뇌신경·심리 기전 정리)
4. <div class="checklist-box"><h3>...</h3><ul><li>...</li>...</ul></div> (증상 자가점검 체크리스트)
5. <div class="doctor-note"><p>...</p></div>
   ★ 매우 중요: .doctor-note 내부에는 CSS(::before)가 '유종민 원장의 임상 노트 · 한방내과 전문의 · 상담심리학 박사' 헤더를 자동으로 그려줍니다. 따라서 .doctor-note 내부에는 절대로 <h3>, <h4> 같은 헤더 제목이나 '유종민 원장의 임상 노트' 문구를 중복 작성하지 말고, 순수 임상 노트 본문 <p> 문단만 넣으세요.
6. <details class="faq-item"><summary>Q. ...</summary><p>A. ...</p></details> (자주 묻는 질문 3개를 각각 <details class="faq-item"> 아코디언으로 작성)

[의료법 준수 가드레일 (절대 위반 금지)]
- '한방신경정신과 전문의'라는 표현은 절대 쓰지 마세요. 반드시 '한방내과 전문의 · 상담심리학 박사'로만 표기하세요.
- '완치', '100% 치료', '100% 완치' 같은 단정적·과장 표현은 절대 쓰지 말고 '호전', '회복', '증상 완화', '조절력 회복' 등으로 정확하게 표현하세요.

[출력 JSON 형식]
{
  "title": "25~40자 이내의 핵심 질환 키워드 포함 제목",
  "meta_description": "120자 이내 검색 결과용 핵심 요약",
  "category": "자율신경실조증 / 공황·불안장애 / 불면증 / 어지럼증 / 틱장애 / ADHD 중 택1",
  "slug": "영문-소문자-하이픈-슬러그",
  "content_text": "content_html의 전체 본문을 순수 텍스트로 정리한 버전 (문단 사이 빈 줄 2개 \\n\\n 구분, AEO 요약·Q&A·CTA 포함, 공백 포함 3,000~4,000자)",
  "content_html": "위 6가지 CSS 컴포넌트와 <h2>, <h3>, <p>, <ul> 태그로 구성된 완성된 에디토리얼 HTML 본문"
}
"""

    step2_prompt = f"""
당신은 한방내과 전문의이자 상담심리학 박사인 해아림한의원 안양점 유종민 대표원장입니다.
아래 Step 1 기획안(아웃라인)을 바탕으로 고급 의학 매거진(에디토리얼) 수준의 심층 칼럼을 집필해 주세요.
참고 원문의 문장을 복사하지 말고, 기획안의 6단계 흐름을 따라 완전히 새롭게 깊이 있는 칼럼을 완성하세요.

{editorial_writing_rules}

[Step 1 기획안 JSON]:
{json.dumps(outline_data, ensure_ascii=False, indent=2)}
"""
    data, _ = call_gemini_json(step2_prompt, step_label="Step 2: 심층 집필·HTML 조립")

    # ==================== STEP 3: 품질 검증 및 1회 자동 보완 ====================
    report = optimizer.analyze_post(data["title"], data["content_text"])
    extra_issues = audit_editorial_quality(data, report)
    all_issues = report["issues"] + extra_issues
    print(
        f"[1차 검증 결과] {data['title']} -> {report['score']}점 / "
        f"분량: {len(data['content_text'].strip())}자 / 이슈: {all_issues}"
    )

    if report["score"] < 80 or all_issues:
        print("[자동 보완 시작] 80점 미만 또는 보완 이슈 발견 -> 1회 자동 보완(Refine)을 실행합니다.")
        refine_prompt = f"""
당신은 한방내과 전문의이자 상담심리학 박사인 해아림한의원 안양점 유종민 대표원장입니다.
아래 1차 작성된 에디토리얼 칼럼 JSON을 검토한 결과, 다음과 같은 품질 보완 사항이 발견되었습니다.
기존 글의 훌륭한 문체와 흐름은 유지하면서, 지적된 모든 이슈와 개선 권장 사항을 빠짐없이 해결하여 완성도를 끌어올린 최종 JSON을 반환해 주세요.

[1차 검증 점수]: {report['score']} / {report['max_score']}점
[반드시 해결해야 할 이슈]:
{json.dumps(all_issues, ensure_ascii=False, indent=2)}
[개선 권장 사항]:
{json.dumps(report['improvements'], ensure_ascii=False, indent=2)}

{editorial_writing_rules}

[1차 작성된 칼럼 JSON]:
{json.dumps(data, ensure_ascii=False, indent=2)}
"""
        refined_data, _ = call_gemini_json(refine_prompt, step_label="Step 3: 품질 자동 보완")
        refined_report = optimizer.analyze_post(refined_data["title"], refined_data["content_text"])
        refined_extra = audit_editorial_quality(refined_data, refined_report)
        print(
            f"[보완 후 최종 검증] {refined_data['title']} -> "
            f"{report['score']}점 -> {refined_report['score']}점 / "
            f"분량: {len(refined_data['content_text'].strip())}자 / "
            f"잔여 이슈: {refined_report['issues'] + refined_extra}"
        )
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