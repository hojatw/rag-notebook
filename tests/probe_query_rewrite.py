"""Measure what query rewrite actually keeps from an over-long question.

Run before deciding how to handle questions that exceed the embedding window.
Three things are unknown without the real chat model, and no mock can answer
them — a fixture would only replay whatever we assumed:

1. Do the rewritten queries stand on their own, or do they need the raw
   question beside them?
2. Do exact terms survive the rewrite? Retrieval needs them twice over: the
   keyword half scores on token overlap, and `QUERY_REWRITE_PROMPT` itself asks
   the model to "prefer exact terms, product names, field names, versions".
3. Where does the current prefix trim cut, on questions whose real ask sits at
   the end?

    NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/python -m tests.probe_query_rewrite

Reads the chat connection from the database (same as the app) and makes ONE
chat call per case. Prints the queries, then a per-case scoreboard comparing
what each strategy retains. Read the verdict at the bottom.

The five cases are shaped to discriminate, not to flatter: two of them are the
patterns where prefix trimming should do worst (the ask at the end, terms
scattered late), one is where it should do best (the ask first), and one has no
explicit question at all.
"""

import asyncio

from app import db, llm
from app.ingest import estimate_embedding_tokens

# Filler that reads like the corpus this deployment actually holds: CMC / 法規 /
# 契約 prose. Long enough to blow a 512-token window several times over.
_CONTRACT_BODY = (
    "本契約依政府採購法及其施行細則辦理，甲方為主辦機關，乙方為得標廠商。"
    "履約期間自決標日起算，乙方應依附件一之工作說明書提供服務，並於每月五日前"
    "提交前月工作報告。工作報告應載明當月實際投入人月、完成項目、未完成項目及"
    "其原因。甲方得於接獲報告後十個工作日內提出書面意見，乙方應於接獲意見後"
    "五個工作日內回覆處理方式。履約標的之智慧財產權歸屬依第十四條辦理。"
    "乙方不得將契約之全部或主要部分轉包予他人，違者甲方得逕行終止契約。"
) * 22

_CMC_BODY = (
    "本節說明原料藥之結構確認資料。結構鑑定採用核磁共振光譜、紅外光譜、"
    "質譜與元素分析等方法，各項結果均符合預期結構。晶型經粉末繞射確認為單一"
    "晶型，未觀察到多晶型轉變。批次分析涵蓋三批連續生產批次，各項規格均符合"
    "既定標準。安定性試驗於長期與加速條件下進行，資料支持所訂之有效期間。"
) * 26

_REGISTRY_BODY = (
    "本登錄研究為多中心、前瞻性、非介入性設計，收案對象為經確診並接受指定"
    "治療之病人。主要觀察指標為治療後運動功能之變化，次要指標包含存活狀況、"
    "住院天數與不良事件發生率。資料收集期間各院依既定時程回報，資料管理中心"
    "負責一致性檢核。統計分析採描述性統計為主，遺漏值不進行插補。"
) * 26


CASES = [
    {
        "name": "A 問句在最後（中文商務最常見的寫法）",
        "why": "前段截斷在這裡應該最吃虧——砍掉的正好是問句本身。",
        "question": (
            "附上契約草案全文供參。\n\n" + _CONTRACT_BODY +
            "\n\n以上是契約內容。請問第十二條所定的驗收不通過罰則，"
            "與第十四條的智慧財產權歸屬有沒有互相衝突的地方？"
        ),
        "must_keep": ["第十二條", "第十四條", "驗收", "罰則", "智慧財產權"],
    },
    {
        "name": "B 精確詞彙密集，且分布在後半",
        "why": "關鍵字檢索靠這些 token 計分；改寫若把它們正規化掉，混合檢索的 0.3 那半就失效。",
        "question": (
            "以下為送審資料節錄。\n\n" + _CMC_BODY +
            "\n\n請確認 JR051 批號在 3.2.S.1.2 章節所載的晶型資料，"
            "與 v2.1 版規格書中 XRPD 的判定條件是否一致；"
            "另請說明 2026-04-17 修訂版與前版的差異。"
        ),
        "must_keep": ["JR051", "3.2.S.1.2", "v2.1", "XRPD", "2026-04-17"],
    },
    {
        "name": "C 一次問三件事，散布在全文",
        "why": "測試改寫會不會只抓到其中一件，把另外兩件丟掉。",
        "question": (
            "第一個問題：這份登錄研究的主要觀察指標是什麼？\n\n" + _REGISTRY_BODY +
            "\n\n第二個問題：收案條件有沒有排除既往接受過基因治療的病人？\n\n" +
            _REGISTRY_BODY +
            "\n\n第三個問題：遺漏值的處理方式為何？"
        ),
        "must_keep": ["主要觀察指標", "收案", "基因治療", "遺漏值"],
    },
    {
        "name": "D 問句在最前（對照組）",
        "why": "前段截斷在這裡應該表現最好。若改寫在這裡也不輸，代表它不是只在特定形狀有效。",
        "question": (
            "請問本登錄研究的次要指標包含哪些項目？以下附上計畫書節錄供參。\n\n"
            + _REGISTRY_BODY
        ),
        "must_keep": ["次要指標"],
    },
    {
        "name": "E 沒有明確問句，只有一句「請說明」",
        "why": "改寫需要自己判斷要查什麼；前段截斷則完全不知道使用者想問什麼。",
        "question": (
            _CMC_BODY + "\n\n請說明。"
        ),
        "must_keep": ["晶型", "安定性"],
    },
]


def _kept(terms: list[str], text: str) -> tuple[list[str], list[str]]:
    kept = [t for t in terms if t in text]
    return kept, [t for t in terms if t not in kept]


async def probe() -> None:
    with db.connect() as conn:
        settings = db.load_llm_settings(conn)
    if not settings.get("chat_model"):
        print("Chat model is not configured — set it under /settings first.")
        return

    budget = llm.embedding_window_budget(settings)
    print(f"provider={settings.get('provider')} model={settings.get('chat_model')}")
    print(f"embedding window in effect: {budget} tokens")
    print(f"(probed value when /settings has measured it, else [diagnostics].embedding_token_budget)")
    print()

    scoreboard = []
    for case in CASES:
        question = case["question"]
        raw_tokens = estimate_embedding_tokens(question)
        print("=" * 78)
        print(case["name"])
        print(f"  {case['why']}")
        print(f"  question: {len(question)} chars, ~{raw_tokens} tokens "
              f"({'over' if raw_tokens > budget else 'within'} the window)")
        print()

        queries = await llm.rewrite_search_queries(question, [], settings)
        # queries[0] is always the raw question: deterministic_hint_queries seeds
        # the list with it before the model's rewrites are appended.
        rewrites = queries[1:]
        # The production path itself, not a lookalike: whatever `embed_texts`
        # would actually send is what this compares against.
        trimmed = llm._fit_to_embedding_window([question], settings, role="query")[0]

        print(f"  rewrite produced {len(rewrites)} queries:")
        for i, q in enumerate(rewrites, 1):
            print(f"    {i}. {q}")
        if not rewrites:
            print("    (none — the rewrite call failed or returned nothing)")
        print()

        joined = " ".join(rewrites)
        kept_r, lost_r = _kept(case["must_keep"], joined)
        kept_t, lost_t = _kept(case["must_keep"], trimmed)

        print(f"  exact terms kept by the rewrites : {len(kept_r)}/{len(case['must_keep'])}"
              f"  {kept_r}")
        if lost_r:
            print(f"    LOST: {lost_r}")
        print(f"  exact terms kept by a prefix trim: {len(kept_t)}/{len(case['must_keep'])}"
              f"  {kept_t}")
        if lost_t:
            print(f"    LOST: {lost_t}")
        print(f"  the trim keeps the first {len(trimmed)} of {len(question)} chars "
              f"({len(trimmed) / len(question) * 100:.0f}%)")
        print()

        scoreboard.append({
            "name": case["name"],
            "total": len(case["must_keep"]),
            "rewrite": len(kept_r),
            "trim": len(kept_t),
            "rewrites": len(rewrites),
        })

    print("=" * 78)
    print("SCOREBOARD — exact terms retained (higher is better)")
    print()
    print(f"  {'case':<40} {'rewrites':>9} {'rewrite':>8} {'trim':>6}")
    for row in scoreboard:
        print(f"  {row['name'][:38]:<40} {row['rewrites']:>9} "
              f"{row['rewrite']}/{row['total']:<6} {row['trim']}/{row['total']}")
    print()

    empty = [r for r in scoreboard if r["rewrites"] == 0]
    rewrite_wins = sum(1 for r in scoreboard if r["rewrite"] > r["trim"])
    trim_wins = sum(1 for r in scoreboard if r["trim"] > r["rewrite"])

    print("VERDICT")
    if empty:
        print(f"  {len(empty)} case(s) produced NO rewrites. Whatever else this shows,")
        print("  the raw question cannot simply be dropped — there has to be a fallback")
        print("  for the case where rewrite returns nothing.")
    print(f"  rewrites retained more exact terms in {rewrite_wins} case(s); "
          f"the prefix trim in {trim_wins}.")
    print()
    print("  Term retention is necessary, not sufficient: it says the keyword half")
    print("  still has something to score on, not that retrieval got better. The")
    print("  question of whether the short queries alone find the right chunks is")
    print("  answered by the Eval Workbench against this deployment's own corpus,")
    print("  not by this script — run the same eval set under each strategy and")
    print("  compare. eval_runs now records the LLM settings, so the two runs are")
    print("  distinguishable in the record.")


if __name__ == "__main__":
    asyncio.run(probe())
