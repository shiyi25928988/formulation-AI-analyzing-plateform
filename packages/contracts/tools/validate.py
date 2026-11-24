#!/usr/bin/env python3
"""AdheFormAI 契约包校验器（零第三方依赖，纯标准库）。

校验五类问题（对应 ITERATION_BACKLOG B04 DoD「支撑契约测试与 OpenAPI diff」）：
1. 语法：全部 JSON 可解析；$ref（相对路径 + JSON Pointer）可解析。
2. Schema：JSON Schema 2020-12 子集内部一致性。
3. OpenAPI：openapi 版本、operationId、responses、POST 强制幂等头与租户头、
   4xx/5xx 统一引用 error.schema.json（Problem Details）。
4. 事件：catalog.json 与 events.schema.json $defs 双向覆盖；信封必填字段齐全。
5. 红线口径：provenance 必返三元组、conformal 三档枚举、OOD 两级枚举、
   任务状态机枚举、九态枚举与 DDL 白名单一致。

用法：
    python packages/contracts/tools/validate.py            # 校验全部
    python packages/contracts/tools/validate.py -v         # 逐条输出
退出码 0 = 全部通过；1 = 存在违规。

支持的 JSON Schema 关键字（子集）：$ref / type / required / properties /
additionalProperties / items / enum / const / minimum / maximum / minLength /
maxLength / pattern / minItems / maxItems / minProperties / format(轻校验) /
description / title / examples / default / $schema / $id / $defs。
遇到不支持的关键字会直接报错（契约包内禁止使用未支持能力，防静默失效）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent.parent

SUPPORTED_KEYWORDS = {
    "$ref", "type", "required", "properties", "additionalProperties", "items",
    "enum", "const", "minimum", "maximum", "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "minProperties", "format", "description", "title",
    "examples", "default", "$schema", "$id", "$defs",
}

NINE_SERVICES = {
    "prediction-svc", "optimization-svc", "active-learning-svc",
    "generation-svc", "experiment-svc", "data-hub-svc", "knowledge-svc",
    "auth-audit-svc", "model-lifecycle-svc",
}

REDLINE_OOD = {"none", "advisory", "severe"}
REDLINE_CONFIDENCE = {0.5, 0.7, 0.9}
REDLINE_TASK_STATUS = {"queued", "running", "succeeded", "failed", "cancelled"}
REDLINE_STATES = {
    "RECOMMENDED", "REVIEWED", "SCHEDULED", "IN_PROGRESS", "COMPLETED",
    "QC_PENDING", "ACCEPTED", "REJECTED", "CANCELLED",
}


class Violations:
    def __init__(self, verbose: bool = False) -> None:
        self.items: list[str] = []
        self.verbose = verbose

    def add(self, msg: str) -> None:
        self.items.append(msg)
        if self.verbose:
            print(f"  VIOLATION: {msg}")

    @property
    def ok(self) -> bool:
        return not self.items


# ---------------------------------------------------------------- $ref 解析
_PTR_ESC = {"~0": "~", "~1": "/"}


def resolve_ref(root_dir: Path, ref: str, depth: int = 0) -> tuple[dict, str]:
    """解析形如 file.schema.json#/pointer 或 #/pointer 的 $ref，返回 (文档, 描述)。"""
    if depth > 12:
        raise ValueError(f"$ref 递归过深: {ref}")
    file_part, _, pointer = ref.partition("#")
    if file_part:
        path = (root_dir / file_part).resolve()
        doc = json.loads(path.read_text(encoding="utf-8"))
        desc = str(path.relative_to(CONTRACTS_DIR))
    else:
        doc = None  # 由调用方提供当前文档
        desc = "(self)"
    node: object = doc
    if pointer:
        if not pointer.startswith("/"):
            raise ValueError(f"仅支持 JSON Pointer 形式: {ref}")
        for raw in pointer.lstrip("/").split("/"):
            key = raw
            for esc, ch in _PTR_ESC.items():
                key = key.replace(esc, ch)
            if node is None:
                raise ValueError(f"self-ref 但未提供文档: {ref}")
            node = node[key] if isinstance(node, dict) and key in node else None
            if node is None:
                raise ValueError(f"$ref 指针落空: {ref}")
    assert isinstance(node, dict), f"$ref 未指向对象: {ref}"
    return node, f"{desc}{pointer or ''}"


class RefResolver:
    """带当前文档的 $ref 解析器。"""

    def __init__(self, current_doc: dict, current_dir: Path) -> None:
        self.doc = current_doc
        self.dir = current_dir

    def resolve(self, ref: str, depth: int = 0) -> dict:
        if ref.startswith("#"):
            node: object = self.doc
            for raw in ref.lstrip("#/").split("/"):
                if not raw:
                    continue
                key = raw
                for esc, ch in _PTR_ESC.items():
                    key = key.replace(esc, ch)
                node = node[key] if isinstance(node, dict) and key in node else None
                if node is None:
                    raise ValueError(f"$ref 指针落空: {ref}")
            assert isinstance(node, dict), f"$ref 未指向对象: {ref}"
            return node
        node, _ = resolve_ref(self.dir, ref, depth)
        return node


# ------------------------------------------------------ JSON Schema 子集校验
def validate_schema_instance(
    schema: dict, instance: object, resolver: RefResolver,
    path: str, v: Violations,
) -> None:
    """对 instance 按 schema 子集校验；违规写入 v。schema 自身的非法关键字另行检查。"""
    if "$ref" in schema:
        target = resolver.resolve(schema["$ref"])
        validate_schema_instance(target, instance, resolver, path, v)
        return
    for key in schema:
        if key not in SUPPORTED_KEYWORDS:
            v.add(f"schema@{path}: 不支持的 schema 关键字 '{key}'（子集外，防静默失效）")
            return

    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        py_types: list[type] = []
        for tt in types:
            py_types.append({
                "object": dict, "array": list, "string": str,
                "number": (int, float), "integer": int,
                "boolean": bool, "null": type(None),
            }[tt])
        # bool 是 int 子类，先剔除
        def matches(val: object) -> bool:
            return any(
                isinstance(val, pt) and not (pt is not bool and isinstance(val, bool))
                for pt in py_types
            )
        if not matches(instance):
            v.add(f"{path}: 类型 {t} 不匹配（实际 {type(instance).__name__}）")
            return
        if "integer" in types and isinstance(instance, bool):
            v.add(f"{path}: integer 误传 boolean")
            return

    if "const" in schema and instance != schema["const"]:
        v.add(f"{path}: const 不匹配（期望 {schema['const']!r}）")
    if "enum" in schema and instance not in schema["enum"]:
        v.add(f"{path}: 值 {instance!r} 不在枚举内")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            v.add(f"{path}: 长度 < {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            v.add(f"{path}: 长度 > {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            v.add(f"{path}: 不匹配 pattern {schema['pattern']!r}")
        if schema.get("format") == "uuid" and not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(instance)
        ):
            v.add(f"{path}: 非法 uuid")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            v.add(f"{path}: < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            v.add(f"{path}: > maximum {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            v.add(f"{path}: 数组元素 < {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            v.add(f"{path}: 数组元素 > {schema['maxItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, elem in enumerate(instance):
                validate_schema_instance(items, elem, resolver, f"{path}[{i}]", v)

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in instance:
                v.add(f"{path}: 缺必填 '{req}'")
        addl = schema.get("additionalProperties", True)
        for k in instance:
            if k in props:
                validate_schema_instance(props[k], instance[k], resolver, f"{path}.{k}", v)
            elif addl is False:
                v.add(f"{path}: 未定义的额外属性 '{k}'（additionalProperties=false）")
            elif isinstance(addl, dict):
                validate_schema_instance(addl, instance[k], resolver, f"{path}.{k}", v)


# ---------------------------------------------------------------- OpenAPI
def check_openapi(name: str, doc: dict, dir_path: Path, v: Violations) -> None:
    resolver = RefResolver(doc, dir_path)
    if doc.get("openapi") != "3.1.0":
        v.add(f"{name}: openapi 版本须为 3.1.0（实际 {doc.get('openapi')!r}）")
    info = doc.get("info", {})
    if "title" not in info or "version" not in info:
        v.add(f"{name}: info 缺 title/version")
    paths = doc.get("paths", {})
    if not paths:
        v.add(f"{name}: paths 为空")

    def get_param_names(op: dict) -> set[str]:
        names = set()
        for p in op.get("parameters", []):
            node = resolver.resolve(p["$ref"]) if "$ref" in p else p
            if "name" in node and node.get("in") == "header":
                names.add(node["name"])
        return names

    for path, item in paths.items():
        for method, op in item.items():
            if method.startswith("x-") or method == "parameters":
                continue
            if "operationId" not in op:
                v.add(f"{name} {method.upper()} {path}: 缺 operationId")
            if "responses" not in op:
                v.add(f"{name} {method.upper()} {path}: 缺 responses")
            headers = get_param_names(op)
            if "X-Tenant-Id" not in headers:
                v.add(f"{name} {method.upper()} {path}: 缺 X-Tenant-Id 头")
            if method == "post" and "Idempotency-Key" not in headers:
                v.add(f"{name} POST {path}: 缺 Idempotency-Key 头（§4.3 所有 POST 必带）")
            for code, resp in op.get("responses", {}).items():
                node = resolver.resolve(resp["$ref"]) if "$ref" in resp else resp
                if code.startswith(("4", "5")):
                    content = node.get("content", {})
                    media = content.get("application/problem+json")
                    if media is None:
                        v.add(f"{name} {code} {path}: 4xx/5xx 未用 application/problem+json")
                    else:
                        sch = media.get("schema", {})
                        target = resolver.resolve(sch["$ref"]) if "$ref" in sch else sch
                        if "$id" not in target or "error.schema.json" not in str(target.get("$id", "")):
                            v.add(f"{name} {code} {path}: 错误响应未引用 error.schema.json")


# ---------------------------------------------------------------- 事件
def check_events(v: Violations) -> None:
    cat_path = CONTRACTS_DIR / "events" / "catalog.json"
    ev_path = CONTRACTS_DIR / "events" / "events.schema.json"
    catalog = json.loads(cat_path.read_text(encoding="utf-8"))
    events_doc = json.loads(ev_path.read_text(encoding="utf-8"))

    cat_types = [e["type"] for e in catalog["events"]]
    defs = set(events_doc.get("$defs", {}))
    dup = {t for t in cat_types if cat_types.count(t) > 1}
    if dup:
        v.add(f"catalog: 事件类型重复 {sorted(dup)}")
    missing_def = set(cat_types) - defs
    if missing_def:
        v.add(f"catalog 有 $defs 缺失的事件: {sorted(missing_def)}")
    unused_def = defs - set(cat_types)
    if unused_def:
        v.add(f"events.schema.json 有未登记目录的 $defs: {sorted(unused_def)}")

    resolver = RefResolver(events_doc, ev_path.parent)
    for entry in catalog["events"]:
        ref = entry.get("payload_ref", "")
        try:
            payload_schema = resolver.resolve(ref)
        except Exception as exc:  # noqa: BLE001
            v.add(f"catalog {entry['type']}: payload_ref 不可解析 {ref}（{exc}）")
            continue
        for field in ("publisher", "consumers", "delivery"):
            if field not in entry:
                v.add(f"catalog {entry['type']}: 缺 {field}")
        if entry.get("publisher") not in NINE_SERVICES:
            v.add(f"catalog {entry['type']}: publisher 非九服务之一（{entry.get('publisher')!r}）")
        for consumer in entry.get("consumers", []):
            if consumer not in NINE_SERVICES:
                v.add(f"catalog {entry['type']}: consumer 非九服务之一（{consumer!r}）")
        if entry.get("delivery") not in ("at-least-once-idempotent", "effective-exactly-once"):
            v.add(f"catalog {entry['type']}: 未知投递语义 {entry.get('delivery')!r}")

    # 信封必填与红线
    envelope_path = CONTRACTS_DIR / "schemas" / "envelope.schema.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    required_env = {"id", "source", "type", "spec_version", "time", "tenant_id",
                    "correlation_id", "idempotency_key", "schema_version", "data"}
    if set(envelope.get("required", [])) != required_env:
        v.add(f"envelope: required 与 §16.4 信封字段不一致（实际 {envelope.get('required')}）")
    if "events" in catalog and catalog.get("idempotency_key_spec") != \
            "(tenant_id, event_type, aggregate_id, aggregate_version)":
        v.add("catalog: 幂等键规范与 §16.4 不一致")

    # 事件 payload 对 envelope.data 的样例校验（每事件构造最小合法样例）
    for entry in catalog["events"]:
        schema = resolver.resolve(entry["payload_ref"])
        sample = minimal_sample(schema, resolver)
        rv = Violations()
        validate_schema_instance(schema, sample, resolver, entry["type"], rv)
        if not rv.ok:
            for item in rv.items:
                v.add(f"事件 {entry['type']} 最小样例未通过自身 schema: {item}")


def minimal_sample(schema: dict, resolver: RefResolver, depth: int = 0) -> object:
    """从 schema 生成最小合法样例（供自检）。"""
    if depth > 12:
        return "…"
    if "$ref" in schema:
        return minimal_sample(resolver.resolve(schema["$ref"]), resolver, depth + 1)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "examples" in schema and schema["examples"]:
        return schema["examples"][0]
    if "default" in schema:
        return schema["default"]
    t = schema.get("type", "string")
    types = t if isinstance(t, list) else [t]
    if "object" in types:
        out = {}
        for k, sub in schema.get("properties", {}).items():
            if k in schema.get("required", []):
                out[k] = minimal_sample(sub, resolver, depth + 1)
        return out
    if "array" in types:
        item = schema.get("items", {"type": "string"})
        return [minimal_sample(item, resolver, depth + 1)]
    if "number" in types or "integer" in types:
        return schema.get("minimum", 1)
    if "boolean" in types:
        return True
    if "null" in types:
        return None
    return "text"


# ---------------------------------------------------- 预测结果语义条件校验
def semantic_check_prediction(instance: object, path: str = "prediction-result") -> list[str]:
    """prediction-result 的条件语义（结构校验之外的红线，§4.3 / §15.3）：
    - ood_level=severe：value / intervals / explanation 必须缺省（拒答不返回数值）；
    - ood_level=advisory：advisory_only 必须为 true；
    - ood_level ∈ {none, advisory} 且返回数值：intervals 三档必须齐全。
    """
    errs: list[str] = []
    if not isinstance(instance, dict):
        return [f"{path}: 非对象"]
    ood = instance.get("ood_level")
    if ood == "severe":
        for field in ("value", "intervals", "explanation"):
            if field in instance and instance[field] is not None:
                errs.append(f"{path}: ood_level=severe 时不得返回 {field}（拒答红线 §4.3）")
    elif ood == "advisory":
        if instance.get("advisory_only") is not True:
            errs.append(f"{path}: ood_level=advisory 时 advisory_only 必须为 true")
    if ood in ("none", "advisory") and instance.get("value") is not None:
        intervals = instance.get("intervals")
        if not isinstance(intervals, list) or len(intervals) != 3:
            errs.append(f"{path}: 返回数值时 intervals 三档必须齐全（§5.2）")
        else:
            confs = sorted(iv.get("confidence") for iv in intervals if isinstance(iv, dict))
            if confs != [0.5, 0.7, 0.9]:
                errs.append(f"{path}: intervals 档位须为 0.50/0.70/0.90（实际 {confs}）")
    return errs


# ---------------------------------------------------------------- 红线
def check_redlines(v: Violations) -> None:
    sch_dir = CONTRACTS_DIR / "schemas"

    interval = json.loads((sch_dir / "interval.schema.json").read_text(encoding="utf-8"))
    resolver = RefResolver(interval, sch_dir)
    conf_node = interval.get("properties", {}).get("confidence", {})
    conf = resolver.resolve(conf_node["$ref"]) if "$ref" in conf_node else conf_node
    if set(conf.get("enum", [])) != REDLINE_CONFIDENCE:
        v.add(f"红线: interval confidence 枚举须为 {sorted(REDLINE_CONFIDENCE)}（§5.2 三档）")

    pred = json.loads((sch_dir / "prediction-result.schema.json").read_text(encoding="utf-8"))
    props = pred.get("properties", {})
    ood = props.get("ood_level", {})
    if ood.get("$ref", "").endswith("OodLevel"):
        common = json.loads((sch_dir / "common.schema.json").read_text(encoding="utf-8"))
        ood_enum = set(common["$defs"]["OodLevel"].get("enum", []))
        if ood_enum != REDLINE_OOD:
            v.add(f"红线: ood_level 枚举须为 {sorted(REDLINE_OOD)}（两级处置）")
    else:
        v.add("红线: prediction-result.ood_level 须引用 common.OodLevel")
    # intervals 不进 required：severe 拒答时整体缺省；语义由条件校验兜底（见下）
    # 条件语义自检：三种 OOD 档位样例必须各归其位
    good_none = {"metric": "m", "unit": "u", "ood_level": "none", "value": 1.0,
                 "intervals": [{"confidence": 0.5, "p_low": 0.9, "p_high": 1.1},
                               {"confidence": 0.7, "p_low": 0.8, "p_high": 1.2},
                               {"confidence": 0.9, "p_low": 0.7, "p_high": 1.3}]}
    good_severe = {"metric": "m", "unit": "u", "ood_level": "severe"}
    bad_severe = good_none | {"ood_level": "severe"}
    bad_advisory = good_none | {"ood_level": "advisory"}
    if semantic_check_prediction(good_none) or semantic_check_prediction(good_severe):
        v.add("红线: prediction 语义条件校验对合法样例误报")
    if not semantic_check_prediction(bad_severe) or not semantic_check_prediction(bad_advisory):
        v.add("红线: prediction 语义条件校验未拦截 severe 返值 / advisory 缺标记")

    prov = json.loads((sch_dir / "provenance.schema.json").read_text(encoding="utf-8"))
    prov_req = set(prov.get("required", []))
    if not {"model_version", "feature_set_version", "dataset_snapshot_id"} <= prov_req:
        v.add(f"红线: provenance 三元组必返（实际 required={sorted(prov_req)}）")

    task = json.loads((sch_dir / "task.schema.json").read_text(encoding="utf-8"))
    task_resolver = RefResolver(task, sch_dir)
    status = task.get("properties", {}).get("status", {})
    status_node = task_resolver.resolve(status["$ref"]) if "$ref" in status else status
    if set(status_node.get("enum", [])) != REDLINE_TASK_STATUS:
        v.add("红线: 任务状态机枚举与 §4.3 不一致")
    if set(task.get("properties", {}).get("queue", {}).get("enum", [])) != \
            {"feature", "batch", "train", "export"}:
        v.add("红线: 队列枚举须为四队列隔离（§15.2）")

    common = json.loads((sch_dir / "common.schema.json").read_text(encoding="utf-8"))
    if set(common["$defs"]["ExperimentState"]["enum"]) != REDLINE_STATES:
        v.add("红线: 九态枚举与 §6.1 / DDL 白名单不一致")


# ---------------------------------------------------------------- 主流程
def main() -> int:
    verbose = "-v" in sys.argv
    v = Violations(verbose)

    # 1) 全部 JSON 可解析 + schema 关键字子集检查
    json_files = sorted(CONTRACTS_DIR.rglob("*.json"))
    print(f"[1/5] JSON 解析与 schema 关键字子集：{len(json_files)} 个文件")
    for path in json_files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            v.add(f"{path.relative_to(CONTRACTS_DIR)}: JSON 解析失败 {exc}")
            continue
        if path.parent.name == "schemas" or path.name == "events.schema.json":
            _walk_keywords(doc, path.relative_to(CONTRACTS_DIR), v)

    # 2) OpenAPI 结构
    print("[2/5] OpenAPI 3.1 结构与红线头")
    specs = sorted((CONTRACTS_DIR / "openapi").glob("*.json"))
    spec_names = {p.stem for p in specs}
    if spec_names != NINE_SERVICES:
        v.add(f"openapi/: 服务清单与 §4.1 九服务不一致（多 {spec_names - NINE_SERVICES}，缺 {NINE_SERVICES - spec_names}）")
    for path in specs:
        doc = json.loads(path.read_text(encoding="utf-8"))
        check_openapi(path.name, doc, path.parent, v)

    # 3) 事件
    print("[3/5] 事件目录与 payload schema 互检")
    check_events(v)

    # 4) 红线
    print("[4/5] 红线口径（provenance / 三档 / OOD / 状态机）")
    check_redlines(v)

    # 5) 全文件 $ref 可达性
    print("[5/5] $ref 相对可达性")
    for path in json_files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        _walk_refs(doc, path.parent, path.relative_to(CONTRACTS_DIR), v)

    print()
    if v.ok:
        print("PASS：契约包校验全部通过。")
        return 0
    print(f"FAIL：{len(v.items)} 项违规：")
    for item in v.items:
        print(f"  - {item}")
    return 1


def _walk_keywords(node: object, rel: Path, v: Violations) -> None:
    """仅对「schema 形状」的节点做关键字子集检查：
    含 type/properties/items/$ref/enum/const 之一的 dict 视为 schema 节点，
    递归进入 properties 值 / items / additionalProperties(dict) / $defs 值；
    属性名映射本身（properties 的键）不是关键字，不得误报。"""
    if isinstance(node, dict):
        is_schema = bool(
            {"type", "properties", "items", "$ref", "enum", "const", "$defs"}
            & set(node.keys())
        )
        if is_schema:
            for k in node:
                if k not in SUPPORTED_KEYWORDS:
                    v.add(f"{rel}: 不支持的 schema 关键字 '{k}'（子集外，防静默失效）")
        for k, sub in node.items():
            if k in ("properties", "$defs") and isinstance(sub, dict):
                for val in sub.values():
                    _walk_keywords(val, rel, v)
            elif k == "items" and isinstance(sub, dict):
                _walk_keywords(sub, rel, v)
            elif k == "additionalProperties" and isinstance(sub, dict):
                _walk_keywords(sub, rel, v)
            elif k not in ("properties", "$defs", "items", "additionalProperties"):
                _walk_keywords(sub, rel, v)
    elif isinstance(node, list):
        for sub in node:
            _walk_keywords(sub, rel, v)


def _walk_refs(node: object, base_dir: Path, rel: Path, v: Violations) -> None:
    if isinstance(node, dict):
        for k, sub in node.items():
            if k == "$ref" and isinstance(sub, str) and not sub.startswith("#"):
                try:
                    resolve_ref(base_dir, sub)
                except Exception as exc:  # noqa: BLE001
                    v.add(f"{rel}: $ref 不可达 {sub}（{exc}）")
            else:
                _walk_refs(sub, base_dir, rel, v)
    elif isinstance(node, list):
        for sub in node:
            _walk_refs(sub, base_dir, rel, v)


if __name__ == "__main__":
    sys.exit(main())
