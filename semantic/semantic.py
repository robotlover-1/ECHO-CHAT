from nuxt import route, logger, Request
from nuxt.repositorys.validation import fields, use_args
import traceback
from parse import parse, PARSER_VERSION
from ontology import ONTOLOGY_VERSION
from embedding import embed_text
from decision import decide

@route("/embed", methods=["POST"])
@use_args({"text": fields.Str(required=True)}, location="json")
def get_embedding(req: Request, args: dict):
    try:
        text = args["text"]
        q = parse(text)
        return {
            "code": 200,
            "embedding": embed_text(text),
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
def get_rerank(req: Request, args: dict):
    try:
        score, shared, reason = decide(parse(args["query"]), parse(args["cached_query"]))
        return {"code": 200, "score": score, "shared": shared, "reason": reason}
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}

@route("/healthz", methods=["GET"])
def healthz(req: Request):
    """健康检查：供 compose healthcheck 与排障使用。不改 /embed /rerank 契约。"""
    return {
        "status": "ok",
        "service": "semantic",
        "embedding_type": "fnv_hash",
        "dimension": 256,
        "parser_version": "v1",
    }
