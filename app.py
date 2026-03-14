from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from supabase import Client, create_client


APP_TITLE = "流感疫苗立场人工标注"
DATA_PATH = Path("data/reference_manual_prepare.csv")
PAGE_SIZE = 1000
UPSERT_MAX_RETRIES = 3
UPSERT_RETRY_SLEEP_SECONDS = 1.2

LABEL_MAP = {
    1: "支持接种",
    2: "无立场",
    3: "延迟接种",
    4: "拒绝接种",
}


def init_page() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="📝", layout="wide")
    st.markdown(
        """
        <style>
            .block-container {padding-top: 1rem; padding-bottom: 2rem;}
            .text-box {
                border: 1px solid #d9d9d9;
                border-radius: 10px;
                padding: 12px;
                min-height: 260px;
                max-height: 58vh;
                overflow-y: auto;
                white-space: pre-wrap;
                line-height: 1.55;
                font-size: 18px;
            }
            div.stButton > button {
                width: 100%;
                min-height: 56px;
                font-size: 20px !important;
                font-weight: 700 !important;
                border-radius: 10px;
            }
            .hint {
                font-size: 14px;
                color: #666;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_resource
def get_supabase_client() -> Client:
    if "SUPABASE_URL" not in st.secrets or "SUPABASE_KEY" not in st.secrets:
        raise RuntimeError("缺少 Supabase 配置。请在 secrets 中设置 SUPABASE_URL 和 SUPABASE_KEY。")
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


def _norm_col_name(s: str) -> str:
    return s.strip().lower()


def _resolve_col(columns: list[str], candidates: list[str]) -> str | None:
    norm_map = {_norm_col_name(c): c for c in columns}
    for c in candidates:
        if _norm_col_name(c) in norm_map:
            return norm_map[_norm_col_name(c)]
    return None


@st.cache_data(show_spinner=False)
def load_samples(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"样本文件不存在: {path.resolve()}")

    last_error: Exception | None = None
    df: pd.DataFrame | None = None
    for enc in ["utf-8-sig", "utf-8", "gb18030", "gbk"]:
        try:
            df = pd.read_csv(path, dtype=str, encoding=enc, low_memory=False)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if df is None:
        raise RuntimeError(f"无法读取样本文件: {path}") from last_error

    id_col = _resolve_col(list(df.columns), ["id", "ID", "Id"])
    text_col = _resolve_col(list(df.columns), ["text", "Text", "TEXT"])
    title_col = _resolve_col(list(df.columns), ["标题", "title", "Title"])
    body_col = _resolve_col(list(df.columns), ["正文", "content", "Content", "body", "Body"])

    if id_col is None:
        raise ValueError("样本文件缺少 id 列。")
    if text_col is None and title_col is None and body_col is None:
        raise ValueError("样本文件缺少 text 列，且找不到可拼接的 标题/正文 列。")

    out = pd.DataFrame()
    out["id"] = df[id_col].fillna("").astype(str).str.strip()

    if text_col is not None:
        text_series = df[text_col].fillna("").astype(str)
    else:
        title = df[title_col].fillna("").astype(str) if title_col else ""
        body = df[body_col].fillna("").astype(str) if body_col else ""
        if isinstance(title, str):
            title = pd.Series([""] * len(df))
        if isinstance(body, str):
            body = pd.Series([""] * len(df))
        text_series = pd.Series(
            [
                f"标题：{t.strip()}\n正文：{b.strip()}" if t.strip() and b.strip() else (t.strip() or b.strip())
                for t, b in zip(title, body)
            ]
        )

    text_series = (
        text_series.fillna("")
        .astype(str)
        .str.replace("\r\n", "\n", regex=False)
        .str.replace("\r", "\n", regex=False)
        .str.strip()
    )

    out["text"] = text_series
    out = out.drop_duplicates(subset=["id"], keep="first").reset_index(drop=True)
    return out


def fetch_annotations_for_annotator(client: Client, annotator_name: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = (
            client.table("annotations")
            .select("annotator_name,sample_id,human_label,labeled_at,updated_at")
            .eq("annotator_name", annotator_name)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        data = resp.data or []
        rows.extend(data)
        if len(data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    if not rows:
        return pd.DataFrame(columns=["annotator_name", "sample_id", "human_label", "labeled_at", "updated_at"])
    return pd.DataFrame(rows)


def explain_upsert_error(exc: Exception) -> str:
    message = str(exc)
    lower = message.lower()
    if "ssl" in lower or "unexpected eof" in lower or "tls" in lower:
        return (
            "保存失败：到 Supabase 的 TLS/SSL 连接被中断。"
            " 这通常是临时网络问题，请稍后重试。"
        )
    if "timeout" in lower or "timed out" in lower:
        return "保存失败：请求超时，请稍后重试。"
    if "annotations" in lower and "schema cache" in lower:
        return "保存失败：Supabase 中尚未正确创建 annotations 表。"
    return f"保存失败：{message}"


def upsert_annotation(client: Client, annotator_name: str, sample_id: str, text: str, human_label: int) -> None:
    payload = {
        "annotator_name": annotator_name,
        "sample_id": sample_id,
        "text_content": text,
        "human_label": int(human_label),
    }

    last_error: Exception | None = None
    for attempt in range(1, UPSERT_MAX_RETRIES + 1):
        try:
            client.table("annotations").upsert(payload, on_conflict="annotator_name,sample_id").execute()
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < UPSERT_MAX_RETRIES:
                time.sleep(UPSERT_RETRY_SLEEP_SECONDS * attempt)

    assert last_error is not None
    raise RuntimeError(explain_upsert_error(last_error)) from last_error


def _next_unlabeled_index(samples: pd.DataFrame, labeled_ids: set[str], start: int = 0) -> int:
    n = len(samples)
    for i in range(max(start, 0), n):
        if samples.at[i, "id"] not in labeled_ids:
            return i
    return n


def _init_annotator_state(annotator: str, samples: pd.DataFrame, ann_df: pd.DataFrame) -> None:
    st.session_state.annotator_name = annotator
    st.session_state.annotation_map = {
        str(r["sample_id"]): int(r["human_label"])
        for _, r in ann_df.iterrows()
        if str(r.get("sample_id", "")).strip() and str(r.get("human_label", "")).strip()
    }
    next_idx = _next_unlabeled_index(samples, set(st.session_state.annotation_map.keys()), start=0)
    st.session_state.current_index = min(next_idx, max(len(samples) - 1, 0))


def _render_admin_section(client: Client) -> None:
    with st.expander("管理员：汇总与导出（可选）", expanded=False):
        st.caption("此区域用于查看各标注员进度并导出全部标注记录。")
        if st.button("加载标注汇总", use_container_width=True):
            rows: list[dict[str, Any]] = []
            offset = 0
            while True:
                resp = (
                    client.table("annotations")
                    .select("annotator_name,sample_id,human_label,updated_at")
                    .range(offset, offset + PAGE_SIZE - 1)
                    .execute()
                )
                data = resp.data or []
                rows.extend(data)
                if len(data) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE

            if not rows:
                st.info("当前无标注数据。")
            else:
                df_all = pd.DataFrame(rows)
                stat = (
                    df_all.groupby("annotator_name", dropna=False)
                    .agg(labeled_count=("sample_id", "nunique"))
                    .reset_index()
                    .sort_values("labeled_count", ascending=False)
                )
                st.dataframe(stat, use_container_width=True, hide_index=True)
                csv_bytes = df_all.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                st.download_button(
                    "下载全部标注结果 CSV",
                    data=csv_bytes,
                    file_name="annotations_export.csv",
                    mime="text/csv",
                    use_container_width=True,
                )


def main() -> None:
    init_page()
    st.title(APP_TITLE)
    st.caption("标签选项：支持接种 | 无立场 | 延迟接种 | 拒绝接种")

    try:
        samples = load_samples(DATA_PATH)
    except Exception as exc:  # noqa: BLE001
        st.error(f"样本加载失败：{exc}")
        st.stop()

    try:
        client = get_supabase_client()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Supabase 初始化失败：{exc}")
        st.stop()

    if "annotator_name" not in st.session_state:
        st.session_state.annotator_name = ""
    if "current_index" not in st.session_state:
        st.session_state.current_index = 0
    if "annotation_map" not in st.session_state:
        st.session_state.annotation_map = {}

    if not st.session_state.annotator_name:
        with st.container(border=True):
            st.subheader("开始 / 继续")
            name_input = st.text_input("请输入标注员姓名或唯一代号", placeholder="例如：annotator_A")
            if st.button("开始 / 继续", type="primary", use_container_width=True):
                annotator = name_input.strip()
                if not annotator:
                    st.warning("请先输入标注员姓名或代号。")
                    st.stop()
                ann_df = fetch_annotations_for_annotator(client, annotator)
                _init_annotator_state(annotator, samples, ann_df)
                st.rerun()
        _render_admin_section(client)
        st.stop()

    annotator = st.session_state.annotator_name
    annotation_map: dict[str, int] = st.session_state.annotation_map
    total = len(samples)
    labeled = len(annotation_map)

    if labeled >= total:
        st.success(f"标注员 {annotator} 已完成全部标注（{labeled}/{total}）。")
        if st.button("重新载入进度", use_container_width=True):
            ann_df = fetch_annotations_for_annotator(client, annotator)
            _init_annotator_state(annotator, samples, ann_df)
            st.rerun()
        _render_admin_section(client)
        st.stop()

    idx = int(st.session_state.current_index)
    idx = min(max(idx, 0), total - 1)
    st.session_state.current_index = idx

    row = samples.iloc[idx]
    sample_id = str(row["id"])
    text = str(row["text"]) if row["text"] is not None else ""
    existing_label = annotation_map.get(sample_id)

    c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
    c1.metric("标注员", annotator)
    c2.metric("当前进度", f"第 {idx + 1} / {total} 条")
    c3.metric("已标注", f"{labeled} / {total}")
    c4.metric("当前样本 id", sample_id)

    if existing_label in LABEL_MAP:
        st.info(f"当前样本已标注：{LABEL_MAP[existing_label]}（可重新选择修改）")
    else:
        st.caption("当前样本尚未标注")

    st.markdown(f"<div class='text-box'>{text}</div>", unsafe_allow_html=True)
    st.markdown("<div class='hint'>点击标签后会立即保存，并自动跳转到下一条。</div>", unsafe_allow_html=True)

    nav_l, nav_r = st.columns([1, 1])
    with nav_l:
        if st.button("上一条", use_container_width=True, disabled=(idx <= 0)):
            st.session_state.current_index = max(0, idx - 1)
            st.rerun()
    with nav_r:
        if st.button("下一条未标注", use_container_width=True):
            next_idx = _next_unlabeled_index(samples, set(annotation_map.keys()), start=idx + 1)
            if next_idx >= total:
                st.success("已到最后，且后续无未标注样本。")
            st.session_state.current_index = min(next_idx, total - 1)
            st.rerun()

    b1, b2, b3, b4 = st.columns(4)
    buttons = [
        (b1, 1, "支持接种"),
        (b2, 2, "无立场"),
        (b3, 3, "延迟接种"),
        (b4, 4, "拒绝接种"),
    ]
    for col, label_code, label_text in buttons:
        with col:
            if st.button(label_text, key=f"label_{label_code}", type="primary", use_container_width=True):
                try:
                    upsert_annotation(client, annotator, sample_id, text, label_code)
                except Exception as exc:  # noqa: BLE001
                    st.error(str(exc))
                    st.stop()

                st.session_state.annotation_map[sample_id] = label_code
                next_idx = _next_unlabeled_index(samples, set(st.session_state.annotation_map.keys()), start=idx + 1)
                st.session_state.current_index = min(next_idx, total - 1)
                st.rerun()

    _render_admin_section(client)


if __name__ == "__main__":
    main()
