"""
ai_advisor.py

기존 암호화 파이프라인(app.py의 /upload)과는 완전히 별개인 AI 분석 레이어입니다.
병원/환자가 데이터를 "복호화한 뒤 열람하는 시점"에만 호출되며, 아래 두 기능을 담당합니다.

  1) 복약 설명 AI  (fetch_dur_taboo_info + generate_medication_explanation)
     - 사실(팩트)은 반드시 식약처 DUR API(공공데이터)에서 가져오고,
       LLM은 그 사실을 "쉬운 말로 풀어 설명"하는 역할만 한다.
     - 이렇게 역할을 분리하는 이유: LLM이 약물 정보를 스스로 생성하면
       환각(hallucination)으로 위험한 오정보가 나올 수 있기 때문.

  2) 건강 통계 리포트 AI (build_health_stats + generate_health_report)
     - 여러 방문(visit) 기록을 pandas로 통계 처리(추세/변화율/이상치)한 뒤,
       그 "숫자 요약값"만 LLM에 넘겨서 자연어 리포트를 쓰게 한다.
     - LLM이 원본 방문 기록 전체나 환자 개인정보를 보는 게 아니라
       이미 계산된 통계치만 보게 해서, 없는 수치를 지어낼 여지를 줄인다.

개인정보 보호 원칙 (중요):
  - 이 모듈의 두 함수(generate_medication_explanation, generate_health_report)는
    "약물명/진단명/통계 수치/생활습관"만 LLM에 전달하고,
    환자 이름·주민번호·연락처 등 식별정보는 절대 인자로 받지 않는다.
  - 호출하는 쪽(app.py)에서 애초에 그런 값을 넘기지 않도록 주의할 것.

DUR API 관련 주의:
  - 아래 DUR_ENDPOINTS 중 '병용금기'(getUsjntTabooInfoList03)는 요청하신 문서에 명시된
    실제 오퍼레이션명입니다.
  - 나머지(특정연령대금기/임부금기/노인주의/투여기간주의)는 같은 서비스군의
    관례적인 명명 패턴을 따랐을 뿐, 제가 공공데이터포털 문서로 직접 확인한 것은
    아닙니다. 반드시 data.go.kr에서 "의약품안전사용서비스(DUR품목정보)" 상세페이지의
    Operation 목록과 대조해서 정확한 오퍼레이션명으로 고쳐 쓰세요.
"""

import os
import json
import requests
import pandas as pd
from anthropic import Anthropic


# ---------------------------------------------------------
# 환경변수 / 클라이언트
# ---------------------------------------------------------

DUR_API_KEY = os.environ.get("DUR_API_KEY")
DUR_BASE_URL = "http://apis.data.go.kr/1471000/DURPrdlstInfoService03"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

_anthropic_client = None


def get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError(
                "환경변수 ANTHROPIC_API_KEY가 설정되어 있지 않습니다 (Claude API 키)."
            )
        _anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


# ---------------------------------------------------------
# 1. DUR API 조회 (공식 데이터 확보 - 사실 담당)
# ---------------------------------------------------------

# operation명 정확도에 대해서는 파일 상단 docstring의 주의사항 참고
DUR_ENDPOINTS = {
    "병용금기": "getUsjntTabooInfoList03",
    "특정연령대금기": "getSpcifyAgeTabooInfoList03",
    "임부금기": "getPwomTabooInfoList03",
    "노인주의": "getOdsnAtentInfoList03",
    "투여기간주의": "getMdctnPdMxmtrmAtentInfoList03",
}


def fetch_dur_taboo_info(item_name, num_of_rows=20):
    """
    식약처 DUR(의약품안전사용서비스) API에서
    병용금기/특정연령대금기/임부금기/노인주의/투여기간주의 정보를 조회한다.

    매개변수:
        item_name : 처방 약물명 (제품명 또는 성분명)

    반환값:
        {카테고리명: [항목, ...] 또는 {"error": ...}}  형태의 dict
        (오퍼레이션 하나가 실패해도 전체가 죽지 않도록 카테고리별로 에러를 담아 반환)
    """
    if not DUR_API_KEY:
        raise RuntimeError(
            "환경변수 DUR_API_KEY가 설정되어 있지 않습니다 (공공데이터포털에서 발급받은 서비스키)."
        )

    results = {}

    for category, operation in DUR_ENDPOINTS.items():
        url = f"{DUR_BASE_URL}/{operation}"
        params = {
            "serviceKey": DUR_API_KEY,
            "itemName": item_name,
            "type": "json",
            "numOfRows": num_of_rows,
            "pageNo": 1,
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            body = data.get("body") or data.get("response", {}).get("body", {})
            items = body.get("items", [])

            # 공공데이터포털 API는 결과가 1건일 때 items.item이 dict로,
            # 여러 건일 때 list로 오는 경우가 흔해서 형태를 맞춰준다.
            if isinstance(items, dict):
                item = items.get("item", [])
                items = item if isinstance(item, list) else ([item] if item else [])

            results[category] = items

        except Exception as e:
            results[category] = {"error": str(e)}

    return results


# ---------------------------------------------------------
# 2. 복약 설명 LLM (설명 담당 - 사실을 새로 만들지 않음)
# ---------------------------------------------------------

MEDICATION_SYSTEM_PROMPT = """당신은 환자에게 복약 정보를 쉬운 말로 설명하는 도우미입니다.
다음 규칙을 반드시 지키세요.

1. 아래 함께 제공되는 '공식 DUR 데이터'에 실제로 들어있는 내용만 근거로 설명하세요.
   공식 데이터에 없는 병용금기·연령금기·임부금기 정보를 새로 만들어내지 마세요.
   해당 카테고리에 데이터가 비어 있으면 "공식 데이터상 특별한 주의사항이 확인되지 않았습니다"
   라고만 말하고, 임의로 채워 넣지 마세요.
2. 전문 용어(예: 병용금기, 노인주의)는 환자가 이해할 수 있는 쉬운 말로 풀어 쓰세요.
3. 진단을 내리거나 처방 변경(복용 중단, 용량 조절 등)을 권유하지 마세요. 이 답변은 의료 자문이 아닙니다.
4. 음식과의 상호작용처럼 공식 DUR 데이터에 없는 내용을 보충 설명할 때는,
   그 부분이 "일반적으로 알려진 참고 정보"임을 명시하고,
   반드시 "정확한 복용법은 의료진·약사와 상담하세요"라는 문구를 포함하세요.
5. 출력은 다음 4개 섹션으로 구성하세요: [효능] [복용법] [주의사항] [병용금기 음식·약물]
"""


def generate_medication_explanation(drug_name, dur_data, diagnosis=None, model="claude-sonnet-4-6"):
    """
    DUR 공식 데이터를 근거로, 환자가 이해하기 쉬운 복약 설명을 생성한다.

    매개변수:
        drug_name : 처방 약물명
        dur_data  : fetch_dur_taboo_info()의 반환값 (공식 데이터)
        diagnosis : (선택) 진단명. 환자 개인정보(이름/ID 등)는 절대 넘기지 말 것.
    """
    client = get_anthropic_client()

    user_content = f"처방 약물명: {drug_name}\n"
    if diagnosis:
        user_content += f"진단명: {diagnosis}\n"
    user_content += (
        f"\n[공식 DUR 데이터]\n{json.dumps(dur_data, ensure_ascii=False, indent=2)}\n"
        "\n위 공식 데이터를 근거로, 환자가 이해하기 쉬운 말로 설명해주세요."
    )

    response = client.messages.create(
        model=model,
        max_tokens=1000,
        system=MEDICATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    return "".join(block.text for block in response.content if block.type == "text")


# ---------------------------------------------------------
# 3. 건강 통계 처리 (pandas - 사실 담당)
# ---------------------------------------------------------

# 통계를 낼 대상 컬럼과, 값이 낮아지는 게 좋은 방향인지(True) 여부.
# 필요한 지표가 늘어나면 여기에 추가하면 된다.
VITAL_COLUMNS = {
    "systolic": {"label": "수축기 혈압", "lower_is_better": True},
    "diastolic": {"label": "이완기 혈압", "lower_is_better": True},
    "glucose": {"label": "혈당", "lower_is_better": True},
}


def build_health_stats(visits):
    """
    여러 번의 방문 기록을 받아 pandas로 추세/변화율/이상치를 계산한다.

    매개변수:
        visits : [{"date": "2026-06-01", "systolic": 130, "diastolic": 85, "glucose": 110}, ...]
                 날짜(date)는 필수, 나머지 지표는 있는 것만 계산한다.

    반환값:
        {
          "visit_count": int,
          "period": {"start": "...", "end": "..."},
          "metrics": {
            "systolic": {
              "first": ..., "last": ..., "mean": ..., "change_rate_percent": ...,
              "trend": "상승"/"하락"/"유지", "outliers": [...]
            },
            ...
          }
        }
    """
    if not visits:
        raise ValueError("visits가 비어 있습니다.")

    df = pd.DataFrame(visits)
    if "date" not in df.columns:
        raise ValueError("각 visit 항목에 'date' 필드가 필요합니다.")

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    metrics = {}
    for col, meta in VITAL_COLUMNS.items():
        if col not in df.columns:
            continue

        series = df[col].dropna()
        if series.empty:
            continue

        first, last = float(series.iloc[0]), float(series.iloc[-1])
        mean = float(series.mean())
        std = float(series.std()) if len(series) > 1 else 0.0

        change_rate = round((last - first) / first * 100, 1) if first else None

        if last > first:
            trend = "상승"
        elif last < first:
            trend = "하락"
        else:
            trend = "유지"

        outliers = (
            series[(series - mean).abs() > 2 * std].round(1).tolist() if std else []
        )

        metrics[col] = {
            "label": meta["label"],
            "first": round(first, 1),
            "last": round(last, 1),
            "mean": round(mean, 1),
            "change_rate_percent": change_rate,
            "trend": trend,
            "outliers": outliers,
        }

    return {
        "visit_count": int(len(df)),
        "period": {
            "start": df["date"].min().strftime("%Y-%m-%d"),
            "end": df["date"].max().strftime("%Y-%m-%d"),
        },
        "metrics": metrics,
    }


# ---------------------------------------------------------
# 4. 건강 리포트 LLM (설명 담당 - 통계 수치를 새로 만들지 않음)
# ---------------------------------------------------------

HEALTH_REPORT_SYSTEM_PROMPT = """당신은 환자용 건강 통계 리포트를 작성하는 도우미입니다.
다음 규칙을 반드시 지키세요.

1. 아래 '통계 요약값'에 있는 숫자만 근거로 사용하세요. 그 안에 없는 수치나 추세를
   새로 만들어내지 마세요.
2. 진단을 내리거나 처방/치료 방법을 지시하지 마세요. 이 리포트는 의료 자문이 아닙니다.
3. 생활습관 관련 조언(예: 저염식, 규칙적인 운동, 재활 지속)은 일반적인 수준의
   참고 제안으로만 제시하세요.
4. 리포트 맨 마지막 줄에 반드시 다음 문구를 그대로 포함하세요:
   "이 리포트는 참고용이며 정확한 진단과 치료는 반드시 의료진과 상담하세요."
"""


def generate_health_report(stats, lifestyle=None, model="claude-sonnet-4-6"):
    """
    build_health_stats()의 통계 요약값(과 선택적으로 생활습관 데이터)을 받아
    자연어 건강 리포트를 생성한다.

    매개변수:
        stats     : build_health_stats()의 반환값
        lifestyle : (선택) {"exercise_frequency": "...", "rehab_status": "..."} 등
                    환자 개인정보(이름/ID 등)는 절대 넘기지 말 것.
    """
    client = get_anthropic_client()

    user_content = f"[통계 요약값]\n{json.dumps(stats, ensure_ascii=False, indent=2)}\n"
    if lifestyle:
        user_content += f"\n[생활습관 데이터]\n{json.dumps(lifestyle, ensure_ascii=False, indent=2)}\n"
    user_content += "\n위 통계를 바탕으로 환자가 이해하기 쉬운 자연어 리포트를 작성해주세요."

    response = client.messages.create(
        model=model,
        max_tokens=1000,
        system=HEALTH_REPORT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    return "".join(block.text for block in response.content if block.type == "text")
