"""
========================================================================================
자율과 자유 클리닉: AI검색(GEO) / AI답변(AEO) / 구글 / 네이버 자동발행글 최적화 검증 엔진
Auto Post SEO/GEO/AEO Optimizer & Validator
========================================================================================
이 스크립트는 네이버 블로그, 웹사이트 칼럼, 티스토리 등에 자동으로 발행되는 글이
1) AI 답변(AEO) 인용 기준 충족 여부 (두괄식 Direct Answer, FAQ 구조)
2) 생성형 AI 검색(GEO) 엔티티 및 팩트 밀도 (진단 기준, 신경계 용어, 신뢰도)
3) 구글 E-E-A-T (전문성, 권위성, 신뢰성)
4) 네이버 C-Rank / D.I.A.+ (자연스러운 문맥, 모바일 가독성, 체류 시간 유도)
를 충족하는지 자동 분석하고, 점수 및 보완 사항과 함께 구조화 데이터를 생성합니다.
"""

import re
import json

class PostOptimizer:
    def __init__(self):
        # 의학/클리닉 핵심 엔티티 및 신뢰도 키워드 (GEO/E-E-A-T 검증용)
        self.medical_entities = [
            "자율신경", "교감신경", "부교감신경", "HRV", "심박변이도",
            "공황장애", "편도체", "불안장애", "ADHD", "전두엽", "뉴로피드백",
            "틱장애", "기저핵", "신체화장애", "한의학", "변증", "기혈순환", "뇌파"
        ]
        
        # AEO (질문 및 답변) 패턴
        self.question_patterns = [
            r"Q\d?[\.:]", r"질문\s*[\.:]", r"\?$", r"원인은\s*무엇인가요", 
            r"치료법은\s*무엇인가요", r"증상은\s*어떤가요", r"어떻게\s*치료하나요"
        ]

    def analyze_post(self, title: str, content: str) -> dict:
        """
        포스팅의 제목과 본문을 분석하여 종합 점수(100점 만점)와 세부 최적화 리포트를 반환합니다.
        """
        report = {
            "title": title,
            "score": 0,
            "max_score": 100,
            "criteria": {},
            "issues": [],
            "improvements": [],
            "schema_ldjson": None
        }

        # 1. 제목 분석 (15점)
        title_len = len(title.strip())
        title_score = 0
        if 20 <= title_len <= 45:
            title_score += 10
            report["criteria"]["title_length"] = "PASS (20~45자 적정 길이 - 구글/네이버 절단 방지)"
        else:
            report["criteria"]["title_length"] = f"FAIL (현재 {title_len}자 - 25~40자 권장)"
            report["issues"].append("제목 길이가 너무 짧거나 길어 검색 결과에서 잘릴 수 있습니다.")
            report["improvements"].append("제목은 [핵심 키워드 + 구체적 질문/해결책 + 대상] 구조로 25~40자 내외로 작성하세요.")

        # 제목에 타깃 질환/키워드 포함 여부
        matched_title_entities = [k for k in self.medical_entities if k in title]
        if matched_title_entities:
            title_score += 5
            report["criteria"]["title_keyword"] = f"PASS (키워드 발견: {', '.join(matched_title_entities)})"
        else:
            report["criteria"]["title_keyword"] = "FAIL (핵심 질환/키워드 누락)"
            report["issues"].append("제목에 검색 타깃 질환명이나 핵심 키워드가 누락되었습니다.")

        report["score"] += title_score

        # 2. AEO (Answer Engine Optimization) - 직접 답변 및 FAQ 구조 (25점)
        aeo_score = 0
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        
        # 두괄식 핵심 답변 존재 여부 (첫 3개 문단 내에 [핵심 요약] 또는 직접 답변 형태가 있는지)
        first_section = " ".join(paragraphs[:3]) if paragraphs else ""
        has_direct_answer = any(marker in first_section for marker in [
            "결론적으로", "요약하면", "핵심은", "[요약]", "[핵심]", "원인은 바로", "정의합니다", "의미합니다"
        ])
        if has_direct_answer:
            aeo_score += 15
            report["criteria"]["aeo_direct_answer"] = "PASS (서두에 AI 답변 엔진이 인용하기 좋은 명확한 두괄식 요약문 존재)"
        else:
            report["criteria"]["aeo_direct_answer"] = "FAIL (서두 두괄식 요약 부재)"
            report["issues"].append("글의 서두에 질문에 대한 즉각적인 단답/요약(Direct Answer)이 없어 AI 챗봇이 인용하기 어렵습니다.")
            report["improvements"].append("서론 첫 3줄 안에 '[핵심 요약] OO질환은 ~한 원인으로 발생하며 ~로 치료합니다' 형태의 명확한 정의를 배치하세요.")

        # FAQ (Q&A) 문답 형태 포함 여부
        has_faq = any(re.search(pat, content) for pat in self.question_patterns)
        if has_faq:
            aeo_score += 10
            report["criteria"]["aeo_faq_structure"] = "PASS (Q&A 또는 질문형 소제목 구조 발견)"
        else:
            report["criteria"]["aeo_faq_structure"] = "FAIL (Q&A 구조 누락)"
            report["issues"].append("본문 내에 환자가 궁금해할 만한 구체적 Q&A 구조가 없습니다.")
            report["improvements"].append("본문 중단이나 하단에 '자주 묻는 질문 (FAQ)' 1~2개를 명시하세요. (AI 오버뷰 최우선 인용 대상)")

        report["score"] += aeo_score

        # 3. GEO (Generative Engine Optimization) - 엔티티 및 팩트 밀도 (25점)
        geo_score = 0
        matched_content_entities = [k for k in self.medical_entities if k in content]
        entity_count = len(matched_content_entities)
        
        if entity_count >= 5:
            geo_score += 15
            report["criteria"]["geo_entity_density"] = f"PASS (고밀도 전문 엔티티 {entity_count}개 감지: {', '.join(matched_content_entities[:5])}...)"
        elif entity_count >= 2:
            geo_score += 8
            report["criteria"]["geo_entity_density"] = f"NORMAL (전문 엔티티 {entity_count}개 감지 - 추가 권장)"
            report["improvements"].append("의학적 메커니즘(자율신경, 교감신경, HRV, 전두엽 등) 전문 지식 단어를 더 포함하세요.")
        else:
            report["criteria"]["geo_entity_density"] = "FAIL (전문 엔티티 부족 - 일반적인 이야기만 나열됨)"
            report["issues"].append("AI가 지식 그래프(Knowledge Graph)와 연결할 수 있는 의학적 전문 용어 및 구체적 수치가 부족합니다.")

        # 수치, 검사명, 통계 등 정량 데이터 포함 여부 (AI가 신뢰하는 Fact)
        has_quant_facts = any(c in content for c in ["검사", "HRV", "뇌파", "%", "단계", "기준", "원인"])
        if has_quant_facts:
            geo_score += 10
            report["criteria"]["geo_fact_density"] = "PASS (검사법 및 정량 기준 포함)"
        else:
            report["criteria"]["geo_fact_density"] = "FAIL (정량 데이터 부재)"
            report["issues"].append("구체적인 검사 지표(HRV, 뇌파, 임상 기준 등)가 부족하여 신뢰도가 낮게 평가될 수 있습니다.")

        report["score"] += geo_score

        # 4. 네이버 C-Rank / D.I.A.+ & 모바일 가독성 (20점)
        naver_score = 0
        # 문단 길이 체크 (모바일 가독성: 한 문단이 300자 이상 길어지면 이탈률 급증)
        long_paragraphs = [p for p in paragraphs if len(p) > 300]
        if len(long_paragraphs) == 0 and len(paragraphs) >= 5:
            naver_score += 10
            report["criteria"]["naver_readability"] = "PASS (모바일 친화적 2~4줄 단위 단락 구분)"
        else:
            report["criteria"]["naver_readability"] = f"WARN ({len(long_paragraphs)}개의 지나치게 긴 문단 발견)"
            report["improvements"].append("모바일 사용자의 체류 시간을 높이기 위해 문단은 2~3문장 단위로 줄바꿈하세요.")

        # 글의 총 글자수 (네이버/구글 1,500자~2,500자 이상이 최적)
        content_len = len(content.strip())
        if content_len >= 1200:
            naver_score += 10
            report["criteria"]["content_length"] = f"PASS (총 {content_len}자 - 충분한 정보성 분량)"
        elif content_len >= 700:
            naver_score += 5
            report["criteria"]["content_length"] = f"NORMAL (총 {content_len}자 - 다소 짧음, 1,200자 이상 권장)"
        else:
            report["criteria"]["content_length"] = f"FAIL (총 {content_len}자 - 저품질/스팸으로 분류될 위험)"
            report["issues"].append("글자 수가 너무 짧아 네이버 D.I.A.+ 및 구글 검색엔진에서 저품질로 분류될 수 있습니다.")

        report["score"] += naver_score

        # 5. 행동 유도 (CTA: Call To Action) (15점)
        cta_score = 0
        has_cta = any(term in content for term in ["예약", "상담", "문의", "카카오톡", "클리닉", "내원", "홈페이지"])
        if has_cta:
            cta_score += 15
            report["criteria"]["cta_presence"] = "PASS (상담/예약/문의 링크 및 안내 존재)"
        else:
            report["criteria"]["cta_presence"] = "FAIL (CTA 누락)"
            report["issues"].append("글을 다 읽은 환자가 카카오톡 상담이나 예약으로 이어질 수 있는 CTA가 없습니다.")
            report["improvements"].append("글 말미에 '카카오톡 1:1 상담' 및 '온라인 간이 예약' 안내 문구를 필수로 삽입하세요.")

        report["score"] += cta_score

        # 6. JSON-LD 스키마 자동 생성 (웹 칼럼 등록 시 복사해 쓸 수 있는 코드)
        faq_questions = []
        for p in paragraphs:
            if any(re.search(pat, p) for pat in self.question_patterns):
                faq_questions.append(p[:80])

        report["schema_ldjson"] = {
            "@context": "https://schema.org",
            "@type": "BlogPosting",
            "headline": title,
            "description": content[:150].replace("\n", " ") + "...",
            "author": {
                "@type": "MedicalClinic",
                "name": "자율과 자유 클리닉"
            },
            "keywords": ", ".join(matched_content_entities[:6]) if matched_content_entities else "자율신경, 클리닉"
        }

        return report

if __name__ == "__main__":
    optimizer = PostOptimizer()
    
    # 예시 테스트용 샘플 포스팅
    sample_title = "자율신경실조증 증상과 어지럼증, 원인부터 바로잡는 한의 치료법"
    sample_content = """
    검사상 아무런 이상이 없다고 하는데 왜 가슴이 두근거리고 기립 시 어지럼증이 계속될까요?
    
    [핵심 요약] 자율신경실조증은 신체의 혈압, 맥박, 호흡을 자동으로 조절하는 교감신경과 부교감신경의 균형이 무너져 나타나는 신경계 기능 이상입니다.
    
    대표적인 증상으로는 기립성 어지럼증, 심계항진(가슴 두근거림), 숨참, 만성 피로, 소화불량, 상열감 등이 있습니다. 일반 혈액검사나 CT, MRI로는 신경계의 기능적 불균형을 잡아내기 어렵기 때문에 HRV(심박변이도) 검사와 뇌파 스트레스 검사를 통해 정량화된 수치로 자율신경 활성도를 확인해야 합니다.
    
    Q. 자율신경실조증은 어떻게 치료하나요?
    자율과 자유 클리닉에서는 과항진된 교감신경을 진정시키는 체질 맞춤 한약과 두뇌 신경망 안정을 유도하는 뉴로피드백 훈련을 통합하여 신체 자율조절력을 근본 복원합니다.
    
    신경계 불편 증상이 지속되신다면 자율과 자유 클리닉 카카오톡 1:1 상담 또는 온라인 예약으로 편안하게 상담받아 보세요.
    """
    
    result = optimizer.analyze_post(sample_title, sample_content)
    print("="*60)
    print(f"포스팅 제목: {result['title']}")
    print(f"최종 최적화 점수: {result['score']}/{result['max_score']} 점")
    print("="*60)
    print("항목별 검증 결과:")
    for k, v in result['criteria'].items():
        print(f" - {k}: {v}")
    
    if result['issues']:
        print("\n발견된 문제점:")
        for iss in result['issues']:
            print(f" [!] {iss}")
            
    if result['improvements']:
        print("\n개선 권장사항:")
        for imp in result['improvements']:
            print(f" [*] {imp}")
            
    print("\n생성된 Schema.org 구조화 데이터:")
    print(json.dumps(result['schema_ldjson'], ensure_ascii=False, indent=2))
