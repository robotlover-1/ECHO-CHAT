#!/usr/bin/env python3
"""Task6 阈值校准工具：三分切分(dev/cal/test 按主题组) + acceptance_threshold/min_margin 定档。

关键结论（Phase-2/3 已证 / 见 semantic/tests/test_embedding.py 头注与 tools 目录文档）：
  e5（对称 "query:" 前缀）句级平均池化后，绝对 cosine 在 `跨主题` 也饱和(~0.84-0.9)，
  同主题别名 ~0.93-0.99。故**绝对 cosine 对"是否同一条缓存"基本不具分类分辨率**；
  —— 跨主题/跨语义安全由 decision(纯规则, subject 硬门) 先做硬拒。
  —— 因此阈值校准的正确口径（由本集验证）：
      * acceptance(s)只对 <decision-ok 的候选> 生效（VSEARCH topK 内部的 low 兜底）。
      * 负样本（语言/操作/意图/约束/主题冲突）全部被 decision 拦在 acceptance 之外
        → 本数据集不存在"decision-ok 而仍该拒"的负样本可供真分隔 tune。
        ⇒ 扫 acceptance 时(在 decision-ok 正样本集)无须对"误收负"惩罚 → 参数不可由负样本唯一决定；
  → 实际交付一个"极低兜底线"(设为 brief 建议的低位 0.0-0.6 带内的 0.60)，既远低于实测
    真实复用最低 cosine(~0.776)保证 recall≈1.0，又只会拦截真正的低余弦嵌入事故。
    真正的区分交给 decision + min_margin（本 loader/层 min_margin 仍 0.0→交给 VSEARCH/Go 侧验证）。

用法（在 semantic/ 内）：python3 -m tools.calibrate
编码较重（~400 个唯文本 encode_query），结果 JSON 缓存到 tools/.cal_cache.json，
重跑未变文本直接命中；打印直方图分位、三分切分大小、独立 test 一次 recall，并把
RECOMMENDATION 写入 tools/cal_thresholds.json（不入 git）。
"""
import json
import os
import random
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_SEM = os.path.dirname(_HERE)
sys.path.insert(0, _SEM)
sys.path.insert(0, os.path.join(_SEM, "tests", "eval"))

# ---------- 数据 / 依赖 ----------
from retrieval_cases import ROWS, category_summary  # noqa: E402
from decision import hard_decide  # noqa: E402
from parse import parse, normalize  # noqa: E402
from ontology import lookup_subject_id  # noqa: E402

_CACHE = os.path.join(_HERE, ".cal_cache.json")
_OUT = os.path.join(_HERE, "cal_thresholds.json")

MATCH = {"onto_pos", "super_pos"}  # category 视作"应复用"的正侧（decision 过滤后再判 shared）


def _subject(text):
    return lookup_subject_id(normalize(text))


def pair_group(q, c):
    """主题桶 key：防止同概念别名跨折泄漏（同 group 整组进同折）。"""
    sq, sc = _subject(q), _subject(c)
    if sq and sc and sq == sc:
        return sq
    if sq and sc:
        return sq  # 两侧都建档但不同主题 → 主题冲突（decision 拦），归主 subject 桶
    if sq is None and sc is None:
        return "__none__"
    return sq or sc or "__none__"


def _split(seed=20260904, ratio=(0.6, 0.2, 0.2)):
    """组级三分。返回 (dev, cal, test) 每组是 (q,c,cat) 列表。"""
    groups = defaultdict(list)
    for (q, c, cat_) in ROWS:
        groups[pair_group(q, c)].append((q, c, cat_))
    keys = sorted(groups)
    rnd = random.Random(seed)
    rnd.shuffle(keys)
    n = len(keys)
    n_dev = round(n * ratio[0])
    n_cal = round(n * ratio[1])
    dk, ck, tk_ = keys[:n_dev], keys[n_dev:n_dev + n_cal], keys[n_dev + n_cal:]
    dev, cal, test = [], [], []
    for k in keys:
        if k in dk:
            dev += groups[k]
        elif k in ck:
            cal += groups[k]
        else:
            test += groups[k]
    return dev, cal, test


class _Cache:
    def __init__(self, path):
        self.path = path
        self.mem = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.mem = json.load(fh)
            except Exception as e:  # noqa
                print("cache load warn:", e)

    def vec(self, text):
        if text not in self.mem:
            import models
            self.mem[text] = models.encode_query(text)
        return self.mem[text]

    def flush(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.mem, fh)
        os.replace(tmp, self.path)


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def _q(arr, qq):
    if not arr:
        return float("nan")
    s = sorted(arr)
    return s[max(0, min(len(s) - 1, int((len(s) - 1) * qq)))]


def main():
    cache = _Cache(_CACHE)
    print("retrieval rows:", len(ROWS), category_summary())

    dev, cal, test = _split()
    print("splits(pairs)  dev=%d cal=%d test=%d  组数~切 60/20/20"
          % (len(dev), len(cal), len(test)))
    print("  dev:", dict(Counter(x[2] for x in dev)))
    print("  cal:", dict(Counter(x[2] for x in cal)))
    print("  test:", dict(Counter(x[2] for x in test)))

    # 唯一文本编码（一次）
    uniq = list(dict.fromkeys([a for a, _, _ in ROWS] + [b for _, b, _ in ROWS]))
    L = len(uniq)
    for i, t in enumerate(uniq):
        cache.vec(t)
        if (i + 1) % 50 == 0 or i + 1 == L:
            print("  encoded %d/%d" % (i + 1, L), flush=True)
    cache.flush()
    V = cache.mem

    def desc(row):
        q, c, cat = row
        sh, reason, _ = hard_decide(parse(q), parse(c))
        return cat, bool(sh), reason, _cos(V[q], V[c])

    def collect(split_rows):
        shared_pos, shared_neg, raw = [], [], []
        for row in split_rows:
            cat, sh, reason, score = desc(row)
            raw.append((cat, sh, reason, score))
            if sh and cat in MATCH:
                shared_pos.append(score)
            elif sh:
                shared_neg.append(score)
        return raw, shared_pos, shared_neg

    dev_raw, dev_pos, dev_neg = collect(dev)
    cal_raw, cal_pos, cal_neg = collect(cal)
    _, test_pos, test_neg = collect(test)

    # 校准仅在 dev(+cal 校验) 上定参；test 不参与任何选择 —— 只跑一次。
    all_pos = dev_pos + cal_pos
    all_neg_shared = dev_neg + cal_neg  # decision-ok 却该拒的负（本集应≈0）
    print("\n=== decision-ok 正样本 cosine 分布（dev+cal 校准端） ===")
    print("  个数 decision-ok 正样本 pos=%d ；decision-ok 负样本=%d" % (len(all_pos), len(all_neg_shared)))
    if all_pos:
        print("  min=%.4f  p2.5%%=%.4f  p25=%.4f  med=%.4f  p75=%.4f  max=%.4f"
              % (min(all_pos), _q(all_pos, .025), _q(all_pos, .25),
                 _q(all_pos, .5), _q(all_pos, .75), max(all_pos)))
        print("  直方图分位 bins (0.70,0.75,0.80,0.85,0.90,0.95):",
              [sum(1 for s in all_pos if a <= s < b) for a, b in
               zip([0.0, .70, .75, .80, .85, .90],
                   [.70, .75, .80, .85, .90, 1.01])])
    print("  decision-ok 负样本数(应≈0，因负都被决策拦):", len(dev_neg) + len(cal_neg))

    # 统计“经验”最低可接受下限：校准端正样本 p2.5% − margin
    floor = (_q(all_pos, .025) - 0.03) if all_pos else float("nan")

    # ====== 定档结论 ======
    acceptance = 0.60          # 落在 brief 建议的低位兜底带(0.0-0.6)；远低于实测真复用最低 ~0.776
    min_margin = 0.0           # decision+margin 体系 margin 待 VSEARCH/Go 验证，此处保守 0.0
    recall_test = (sum(1 for s in test_pos if s >= acceptance) / len(test_pos)
                   if test_pos else float("nan"))
    recall_fit = (sum(1 for s in all_pos if s >= acceptance) / len(all_pos)
                  if all_pos else float("nan"))
    n_test_pos = len(test_pos)
    n_test_neg_shared = len(test_neg)

    print("\n结论(诚实):")
    print("  - 不存在 decision-ok 而仍该拒的负样本 → 无正/负可分 acceptance(线性扫描不可唯一)。")
    print(f"  - statistical floor(p_2.5-0.03)={floor:.4f}；实际采用保守兜底 acceptance={acceptance}")
    print(f"  - fit recall@{acceptance} (dev+cal正样本) = {recall_fit:.3f}")
    print(f"  - **[独立 test 一次]** 正样本 n={n_test_pos}, recall@{acceptance} = {recall_test:.3f}"
          f"  (test decision-ok 负={n_test_neg_shared})")
    print("  - min_margin=%s（0.0 起步，VSEARCH 阶段再校验 margin 行为）" % min_margin)

    result = {
        "acceptance_threshold": acceptance,
        "min_margin": min_margin,
        "n_rows": len(ROWS),
        "split": {"dev": len(dev), "cal": len(cal), "test": len(test)},
        "cal_set_pos_n": len(all_pos),
        "cal_set_neg_shared_n": len(dev_neg) + len(cal_neg),
        "pos_cos_min": round(min(all_pos), 4) if all_pos else None,
        "pos_cos_p2_5": round(_q(all_pos, .025), 4) if all_pos else None,
        "statistical_floor": round(floor, 4),
        "test_pos_n": n_test_pos,
        "test_recall_at_acceptance": round(recall_test, 4),
    }
    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print("\n结果写入", _OUT)


if __name__ == "__main__":
    main()
