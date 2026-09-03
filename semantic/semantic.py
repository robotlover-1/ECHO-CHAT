"""semantic.py —— 语义服务路由：/embed /rerank(deprecated) /decision[/batch] /healthz /readyz /model-info。"""
from nuxt import route, logger
from nuxt.repositorys.validation import fields, use_args
import traceback
import json
from parse import parse
from embedding import embed_text
from decision import hard_decide
import models  # e5 封装：warmup/ready/model_info/encode_query


def _startup_warmup():
    """启动预加载 + warmup：失败仅记日志不退出（readyz 会反映为 error）。"""
    try:
        models.warmup()
        logger.info("semantic: e5 模型已载+warmed dim=%d ns=%s",
                    models.DIMENSION, models.NAMESPACE)
    except Exception as e:                      # noqa
        logger.error("semantic: warmup 失败(readyz 将反映): %r\n%s",
                     e, traceback.format_exc())


_startup_warmup()


@route("/embed", methods=["POST"])
@use_args({"text": fields.Str(required=True)}, location="json")
def get_embedding(req, args: dict):
    try:
        text = args["text"]
        q = parse(text)
        return {
            "code": 200,
            "embedding": embed_text(text),       # == models.encode_query(text)：384 L2 语义向量
            "bypass_cache": q.bypass_cache,
            "context_dependent": q.context_dependent,
            "intent": q.intent,
            "subject": q.subject_text,
            "subject_id": q.subject_id,
            "language": q.language,
            "operation": q.operation,
            "output_type": q.output_type,
            "fingerprint": q.fingerprint,
            "fingerprint_eligible": q.fingerprint_eligible,
            "parser_version": q.parser_version,
            "ontology_version": q.ontology_version,
        }
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}


@route("/rerank", methods=["POST"])
@use_args({"query": fields.Str(required=True), "cached_query": fields.Str(required=True)}, location="json")
def get_rerank(req, args: dict):
    """DEPRECATED：decision 已纯规则化，不再产模型分；Go 侧改用 /v1/decision[/batch]。
    保留端点返回 {code, score:0.0, shared, reason, soft} 兼容旧结构，score 恒 0.0。
    """
    try:
        shared, reason, soft = hard_decide(parse(args["query"]), parse(args["cached_query"]))
        return {"code": 200, "score": 0.0, "shared": shared, "reason": reason, "soft": soft}
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}


@route("/v1/decision", methods=["POST"])
@use_args({"query": fields.Str(required=True), "cached_query": fields.Str(required=True)}, location="json")
def v1_decision(req, args: dict):
    """单对纯规则决策：{code, shared, reason, soft}，无模型分。"""
    try:
        shared, reason, soft = hard_decide(parse(args["query"]), parse(args["cached_query"]))
        return {"code": 200, "shared": shared, "reason": reason, "soft": soft}
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}


def _json_list(value, name):
    """batch candidates 手解析：优先已是 list；是 JSON 串则解析；否则报错。"""
    if isinstance(value, list):
        return value
    if isinstance(value, (str, bytes)):
        v = json.loads(value)
        if not isinstance(v, list):
            raise ValueError("%s 须为数组" % name)
        return v
    raise ValueError("%s 须为数组或 JSON 数组串" % name)


@route("/v1/decision/batch", methods=["POST"])
@use_args({"query": fields.Str(required=True), "candidates": fields.Raw(required=True)}, location="json")
def v1_decision_batch(req, args: dict):
    """批量决策：逐条 hard_decide；candidates 为数组（List 不可靠兼容 → 手解析 JSON/list）。"""
    try:
        qp = parse(args["query"])
        out = []
        for c in _json_list(args["candidates"], "candidates"):
            s, r, soft = hard_decide(qp, parse(c))
            out.append({"cached_query": c, "shared": s, "reason": r, "soft": soft})
        return {"code": 200, "results": out}
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}


@route("/healthz", methods=["GET"])
def healthz(req):
    """Liveness：进程活着即 ok（不依赖模型）。供 compose healthcheck 与排障。"""
    return {"status": "ok"}


@route("/readyz", methods=["GET"])
def readyz(req):
    """Readiness：模型已载 + warmup 通过；ok=模型信息合并，否=error + 明细。"""
    ok, detail = models.ready()
    if ok:
        return {"status": "ok", **models.model_info(), **detail}
    return {"status": "error", **detail}


@route("/model-info", methods=["GET"])
def model_info(req):
    """{model, revision, dimension, vector_namespace, export_version}：供 Go 启动一致性校验。"""
    return {"code": 200, **models.model_info()}
