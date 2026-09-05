# categorizer.py
"""
팀원 C - 가맹점 카테고리 분류 모듈
가맹점명(merchant_name) 및 MCC 코드를 기반으로 카테고리를 분류합니다.
"""

CATEGORY_MAP = {
    # 🍔 식비
    '스타벅스': '식비', '맥도날드': '식비', '배달의민족': '식비', '김밥천국': '식비',
    
    # 🚕 교통
    '카카오택시': '교통', '지하철': '교통', '버스': '교통', 'KTX': '교통',
    
    # 🛍️ 쇼핑
    '쿠팡': '쇼핑', '프리스비': '쇼핑', '올리브영': '쇼핑', '무신사': '쇼핑',
    'Amazon_US': '쇼핑', 'AliExpress_CN': '쇼핑', '금은방_귀금속': '쇼핑',
    
    # 💻 업무용
    '어도비(Adobe)': '업무용', 'AWS': '업무용', '노션': '업무용', '패스트파이브': '업무용',
    
    # 🏥 의료/건강
    '병원': '의료/건강', '약국': '의료/건강',
    
    # 🎮 기타 / 유흥
    '유흥업소': '기타', 'Steam_Global': '기타', '게임아이템_스토어': '기타', '중고거래_송금': '기타'
}

def categorize_merchant(merchant_name: str) -> str:
    """
    가맹점명을 받아 카테고리를 반환합니다.
    매핑되지 않은 가맹점은 '기타'로 분류합니다.
    """
    if not merchant_name or not isinstance(merchant_name, str):
        return '기타'
    return CATEGORY_MAP.get(merchant_name.strip(), '기타')
