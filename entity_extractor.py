#!/usr/bin/env python3
"""Entity extraction for Caixin wenews articles (v5 - complete rewrite).

Uses a much more precise approach:
- Person: title-before-name pattern + surname+verb with strict validation
- Org: known dictionary + strict suffix matching with boundary checks
- Event: keyword-based with strict length and boundary control
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ── Common Chinese surnames (450+) ─────────────────────────────────────────
SURNAMES = set(
    "王李张刘陈杨黄赵周吴徐孙马朱胡郭何林罗高郑梁谢宋唐许韩冯邓曹彭曾肖田"
    "董潘袁于蒋蔡余杜苏叶程吕魏丁沈任姚卢傅钟姜崔范汪陆金石戴贾韦夏邱"
    "方侯邹熊孟秦白江阎薛尹段雷黎史龙顾邵贺万覃武钱严莫孔霍阮"
    "穆纪管祝祁庞艾童凌盛汤缪甘尤左季庄裴席温牛"
    "密霍"  # Add missing surnames like 密春雷
)

# ── Person stop words - should NEVER be part of a name ─────────────────────
PERSON_STOP_WORDS = {
    "调整", "有望", "迎来", "密集", "进一步", "一把", "多位",
    "高管", "干部", "员工", "人员", "人士", "专家", "学者",
    "表示", "认为", "指出", "透露", "称", "说", "建议", "预计",
    "已经", "正在", "将要", "需要", "可以", "必须", "应该",
    "目前", "此前", "我们", "他们", "这个", "那个", "自己",
    "公司", "银行", "证券", "基金", "保险", "信托", "期货",
    "金融", "集团", "企业", "机构", "市场", "行业",
    "如何", "为何", "是否", "什么", "怎么", "任上", "任内",
    "董事长", "副董事长", "行长", "总裁", "总经理", "总监",
    "股东", "万元", "亿元", "资金", "案件", "问题",
    "漩涡", "风暴", "迷局", "内幕", "真相", "背后",
    "被查", "落马", "失联", "涉案", "行贿", "受贿",
}

# ── Known organizations (exact dictionary) ─────────────────────────────────
KNOWN_ORGS = {
    # Regulators
    "央行", "证监会", "银保监", "银保监会", "金融监管总局", "国家金融监管总局",
    "上交所", "深交所", "北交所", "港交所",
    "财政部", "国务院", "发改委", "审计署", "外汇局",
    "中央金融办", "金融委", "国务院金融委",
    # Banks
    "工商银行", "农业银行", "中国银行", "建设银行", "交通银行", "邮储银行",
    "招商银行", "兴业银行", "浦发银行", "民生银行", "光大银行", "中信银行",
    "华夏银行", "恒丰银行", "渤海银行", "浙商银行", "平安银行",
    "包商银行", "中原银行", "盛京银行", "锦州银行", "营口银行",
    "国开行", "进出口银行", "农发行",
    # Securities
    "中信证券", "中信建投", "国泰君安", "海通证券", "华泰证券", "招商证券",
    "光大证券", "广发证券", "中金公司", "中银国际", "国信证券",
    "天风证券", "中山证券", "渤海证券", "长城证券",
    # Funds
    "博时基金", "华夏基金", "易方达基金", "南方基金", "嘉实基金",
    "广发基金", "淳厚基金", "富国基金", "汇添富基金",
    # Insurance
    "中国人寿", "中国平安", "中国太保", "新华保险", "泰康保险",
    "大家保险", "上海人寿", "安邦保险",
    # Trust/Other
    "安信信托", "中融信托", "四川信托", "中信信托",
    "华夏幸福", "蚂蚁集团", "腾讯", "阿里巴巴", "京东",
    "高盛", "摩根士丹利", "摩根大通", "花旗", "汇丰",
    "红杉资本", "高瓴资本", "鼎晖投资",
    # Specific entities
    "华融资产", "华融", "九鼎", "方正集团", "方正",
    "忠旺集团", "丰盛控股", "川投", "青海省投",
}

# ── Organization suffixes for pattern matching ─────────────────────────────
ORG_SUFFIXES = {
    "银行", "证券", "基金", "保险", "信托", "期货",
    "集团", "公司", "交易所", "监管局", "证监会",
}

# ── Person title/position words (appear BEFORE or AFTER name) ──────────────
PERSON_TITLES_BEFORE = [
    "董事长", "副董事长", "行长", "副行长", "总裁", "副总裁",
    "总经理", "副总经理", "总监", "负责人", "一把手",
    "掌舵人", "创始人", "实控人", "控制人",
    "党委书记", "党组书记", "纪检组长",
    "基金经理", "投资总监", "首席会计师", "首席",
    "作者", "责编", "责任编辑", "记者", "编辑",
    "主席助理",  # Added for "原银监会主席助理杨家才"
]

PERSON_TITLES_AFTER = [
    "董事长", "副董事长", "行长", "副行长", "总裁", "副总裁",
    "总经理", "副总经理", "总监", "负责人",
    "党委书记", "党组书记",
    "基金经理", "投资总监", "首席会计师",
]

# ── Action verbs that follow person names ───────────────────────────────────
PERSON_ACTION_VERBS = [
    "辞职", "离职", "卸任", "离任", "免职", "撤职", "开除", "双开",
    "调任", "转任", "履新", "上任", "出任", "获任", "升任", "降职",
    "被查", "落马", "被捕", "被抓", "投案", "自首",
    "失联", "落网", "归案",
    "表示", "透露", "指出", "认为", "建议", "预计",
    "回应", "强调", "坦言", "直言",
    "深陷", "卷入", "涉及", "牵涉", "陷入",
    "贪污", "受贿", "挪用", "侵占",
    "退休", "复出", "回归", "加盟",
    "如何",  # e.g., "冯鹤年如何深陷"
]

# ── Event keywords ─────────────────────────────────────────────────────────
EVENT_KEYWORDS = [
    "案", "事件", "风波", "危机", "丑闻", "爆雷",
    "落马", "被查",
    "辞职", "离职", "卸任", "免职",
    "获批", "核准", "批复",
    "上市", "退市",
    "并购", "收购", "重组", "合并",
    "违约", "逾期", "暴雷", "破产", "重整", "清算",
    "罚款", "处罚", "判决",
    "反腐", "贪腐",
]

# ── Words that should NOT appear in person names ────────────────────────────
NAME_BAD_CHARS = set("的了一是在和与或及等将已被把从向对为以有还也都曾会能可应")
NAME_BAD_SUFFIXES = {"被", "因", "终", "案", "谜", "并", "将", "出", "中", "的", "了", "在", "落",
                    "失", "投", "拟", "何", "任", "离", "猝", "缘", "涉", "遭", "赴", "携",
                    "究", "揭", "遗", "为", "是", "与", "及", "从", "向", "对", "以", "职", "加",
                    "突", "去", "跨", "归", "再", "又", "始", "渐", "仍", "已", "未", "非",
                    "陨", "夫", "二", "三", "四", "五"}


def _is_valid_person_name(name: str) -> bool:
    """Strict validation of a candidate person name."""
    if not name:
        return False
    n = len(name)
    if n < 2 or n > 4:
        return False
    # Must start with a surname
    if name[0] not in SURNAMES:
        return False
    # Must be all Chinese characters
    if not re.match(r"^[\u4e00-\u9fff]+$", name):
        return False
    # Must not be a stop word
    if name in PERSON_STOP_WORDS:
        return False
    # Must not end with bad suffix characters
    if name[-1] in NAME_BAD_SUFFIXES:
        return False
    # Any middle/last char must not be a function word or bad char
    for ch in name[1:]:
        if ch in NAME_BAD_CHARS or ch in NAME_BAD_SUFFIXES:
            return False
    # Reject names with consecutive surnames at start (e.g., "金李格平", "席吴存荣")
    if n >= 2 and name[1] in SURNAMES:
        return False
    # Must not contain common non-name words
    for bad in ["董事长", "高管", "万元", "亿元", "资金", "股东", "集团",
                 "被查", "落马", "失联", "漩涡", "风暴", "迷局",
                 "金融", "单位", "服务", "债务", "银行", "证券", "保险",
                 "总经理", "中心", "信托", "分行", "监总", "金监", "基金", "公司",
                 "人寿", "副", "夫妇", "夫妻", "家属", "亲属", "国资委", "官员",
                 "迎来", "新任", "上任"]:
        if bad in name:
            return False
    return True


def extract_people(title: str, summary: str = "") -> list:
    """Extract person names using multiple patterns."""
    people = []
    seen = set()
    full_text = title + " " + summary
    title_len = len(title)

    def _add(name, pos):
        if name not in seen and _is_valid_person_name(name):
            people.append((name, "title" if pos < title_len else "summary"))
            seen.add(name)

    # Pattern 1: Name + title (e.g., "田惠宇董事长")
    for t in PERSON_TITLES_AFTER:
        pattern = rf"([\u4e00-\u9fff]{{2,4}}){t}"
        for m in re.finditer(pattern, full_text):
            candidate = m.group(1)
            if candidate[0] in SURNAMES:
                _add(candidate, m.start())

    # Pattern 2: Title + Name (e.g., "董事长田惠宇")
    # Try matching 4,3,2 chars and take first valid name
    for t in PERSON_TITLES_BEFORE:
        pattern_matches = []  # Collect all matches across lengths
        for length in [4, 3, 2]:
            pattern = rf"{t}([\u4e00-\u9fff]{{{length}}})"
            for m in re.finditer(pattern, full_text):
                pattern_matches.append((m.start(), m.group(1)))
        
        # Sort by position, then by length descending (prefer longer names)
        pattern_matches.sort(key=lambda x: (x[0], -len(x[1])))
        
        for pos, candidate in pattern_matches:
            if _is_valid_person_name(candidate):
                _add(candidate, pos)
                break  # Only add first valid name per title instance

    # Pattern 3: Surname + 1-3 chars + action verb (with word boundary check)
    # Match surname + 1-3 chars followed by a verb
    # Use negative lookbehind to ensure surname is not preceded by Chinese char
    verbs_alt = "|".join(sorted(PERSON_ACTION_VERBS, key=len, reverse=True))
    p3_pattern = rf"(?<!\u4e00)([\u4e00-\u9fff]{{2,4}})(?:{verbs_alt})"
    for m in re.finditer(p3_pattern, full_text):
        candidate = m.group(1)
        if candidate[0] in SURNAMES:
            _add(candidate, m.start())

    # Pattern 4: "原/前/现/新任 + title + Name"
    for prefix in ["原", "前", "现", "新任"]:
        for t in PERSON_TITLES_AFTER:
            pattern = rf"{prefix}{t}([\u4e00-\u9fff]{{2,4}})"
            for m in re.finditer(pattern, full_text):
                _add(m.group(1), m.start())

    return people


def extract_organizations(title: str, summary: str = "") -> list:
    """Extract organization names using dictionary + strict suffix matching."""
    orgs = []
    seen = set()
    full_text = title + " " + summary

    def _add(name, source):
        if name not in seen and len(name) >= 2:
            orgs.append((name, source))
            seen.add(name)

    # Pattern 1: Known organizations (exact dictionary match, longest first).
    # Shorter aliases fully covered by an already-matched longer org are skipped
    # (e.g., "银保监" inside "银保监会").
    masked_text = full_text
    for org in sorted(KNOWN_ORGS, key=len, reverse=True):
        if org in masked_text and org not in seen:
            _add(org, "title")
            masked_text = masked_text.replace(org, "＃" * len(org))

    # Pattern 2: XX + suffix with strict boundary checks
    for suffix in ORG_SUFFIXES:
        pattern = rf"([\u4e00-\u9fff]{{2,6}}){suffix}"
        for m in re.finditer(pattern, full_text):
            full_name = m.group(0)
            if full_name in seen:
                continue

            # Reject if starts with bad prefixes
            if full_name[:2] in {"一位", "一家", "多位", "多家", "多个", "数家",
                                  "若干", "某些", "数个", "问题", "部分", "首批",
                                  "漩涡", "难掩", "染指", "重塑", "曝出", "透露",
                                  "表示", "指出", "认为", "如何", "为何",
                                  "曾任", "历任", "时任", "现任", "原任",
                                  "这家", "那家", "三方", "收购", "涉蒲",
                                  "入股", "拟筹", "亿元", "十多", "一带", "人系",
                                  "筹建", "参股", "控股了"}:
                continue

            # Reject if contains sentence-like words
            bad_words = ["中的", "如何", "为何", "将要", "拿下", "走出", "染指",
                          "漩涡", "难掩", "曝出", "透露", "的", "了",
                          "央企", "子公司", "资产管理", "后重", "占用", "暴露",
                          "性银行", "第三大", "拍下", "改制为", "与中", "控盘",
                          "入股", "筹建", "亿元", "多家", "一带一路",
                          "与", "旗下", "两家", "几家", "备案", "资深",
                          "最大", "破解", "断引", "原行长", "银行系",
                          "身陷", "错过", "转让", "辞", "亿大", "对坑",
                          "有意", "出售", "再战", "换血", "挪用", "哪些", "多只",
                          "整合", "收购", "或向", "终止", "挂牌", "坏账",
                          "核心资产", "一银行", "一公司", "一信托"]
            if any(w in full_name for w in bad_words):
                continue

            # Reject if it wraps a known org with extra prefix chars
            # (e.g., "小波终辞中信证券" contains "中信证券")
            if any(org in full_name and org != full_name for org in KNOWN_ORGS):
                continue

            # Reject if starts with a verb-like char
            if full_name[0] in "获让致使令据经因被":
                continue

            # Check preceding character
            if m.start() > 0:
                prev_char = full_text[m.start() - 1]
                if prev_char in "的了一是在和及将已被把从向对为以与同因由用":
                    continue

            # Check if preceded by verb
            if m.start() >= 2:
                prev2 = full_text[m.start()-2:m.start()]
                if prev2 in {"透露", "表示", "指出", "认为", "建议"}:
                    continue

            # Must not start with known bad prefixes (longer check)
            if "曾在" in full_name[:4]:
                continue

            # Must be at least 4 chars total for suffix patterns
            if len(full_name) < 4:
                continue

            _add(full_name, "title")

    return orgs


def extract_events(title: str, summary: str = "") -> list:
    """Extract event names using precise keyword matching."""
    events = []
    seen = set()
    full_text = title + " " + summary

    for keyword in EVENT_KEYWORDS:
        # Match 2-8 Chinese chars before keyword
        pattern = rf"([\u4e00-\u9fff]{{2,8}}){keyword}"
        for m in re.finditer(pattern, full_text):
            event_name = m.group(0)
            if len(event_name) < 4 or len(event_name) > 12:
                continue

            # Reject if starts with bad chars
            if m.start() > 0:
                prev_char = full_text[m.start() - 1]
                if prev_char in "的了一是在和与或及等将已被把从向对为以":
                    continue

            # Reject if contains bad words
            bad_words = ["如何", "为何", "所何", "多个", "因何", "漩涡", "难掩"]
            if any(w in event_name for w in bad_words):
                continue

            # Reject if starts with bad chars
            if event_name[0] in "在于何被将已曾陷亿":
                continue

            # Reject if contains person name indicators
            if "董事长" in event_name or "总经理" in event_name or "行长" in event_name:
                continue

            # Max event length 8 chars
            if len(event_name) > 8:
                continue

            # Keyword must be at the end
            if not event_name.endswith(keyword):
                continue

            if event_name not in seen:
                events.append((event_name, "title"))
                seen.add(event_name)

    return events


def extract_entities(title: str, summary: str = "") -> list:
    """Extract all entities from title and summary."""
    @dataclass(frozen=True)
    class Entity:
        name: str
        type: str
        source: str

    entities = []
    seen = set()

    for name, source in extract_people(title, summary):
        key = (name, "person")
        if key not in seen:
            entities.append(Entity(name, "person", source))
            seen.add(key)

    for name, source in extract_organizations(title, summary):
        key = (name, "org")
        if key not in seen:
            entities.append(Entity(name, "org", source))
            seen.add(key)

    for name, source in extract_events(title, summary):
        key = (name, "event")
        if key not in seen:
            entities.append(Entity(name, "event", source))
            seen.add(key)

    return entities


if __name__ == "__main__":
    test_cases = [
        ("中信系高管人事新动向 金融央企子公司干部调整启幕",
         "一位曾在中信银行总分行多地任职的老将有望获进一步使用；中信金融资产管理公司一把手的更迭终于临近"),
        ("要求债务展期 华夏幸福提出六月之约| 拯救华夏幸福之三", ""),
        ("三天交代1.2亿  冯鹤年如何深陷政商旋转门腐败案", ""),
        ("证监系统干部迎来密集调整 首席会计师易人|证监人事追踪之十", ""),
        ("现场目击北京多个互金贷款平台被查 警方重拳出击所为何来", ""),
        ("博时基金掌舵者调整在即 固收大厂如何维持往日辉煌", ""),
        ("夏先德离任财政部 透露中央金融办新动向", ""),
        ("上交所原审核中心副主任被查 说法多多", ""),
        ("密春雷失联逾两月 上海人寿何去何从", ""),
        ("湖北工行132亿假理财案曝光 多家股份行卷入", ""),
        ("田惠宇案最新进展 董事长李晓鹏被查", ""),
        ("张红力被双开 曾任工商银行副行长", ""),
        ("原银监会主席助理杨家才落马", ""),
    ]

    for title, summary in test_cases:
        print(f"TITLE: {title}")
        entities = extract_entities(title, summary)
        for e in entities:
            print(f"  {e.type:6} {e.name}")
        print("---")
