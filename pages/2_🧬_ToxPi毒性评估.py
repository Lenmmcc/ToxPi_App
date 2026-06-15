import io
import os
import re
import sys

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.toxpi_calc import (  # noqa: E402
    calculate_toxpi,
    generate_multi_toxpi_plot,
    generate_toxpi_bar_plot,
    get_toxic_cols_from_frame,
    load_and_clean_data,
    run_sensitivity_analysis,
    safe_normalize_data,
)


st.set_page_config(
    page_title="ToxPi 毒性评估系统 - ChemPriority",
    page_icon="🧬",
    layout="wide",
)


def clear_cached_data():
    keys_to_del = [
        key
        for key in st.session_state.keys()
        if key in {"cached_df", "cached_filename"}
        or key.startswith("group_comp_")
        or key.startswith("color_group_")
        or key.startswith("saved_weight_")
        or key.startswith("selected_toxic_cols_")
        or key.startswith("text_in_")
        or key.startswith("picker_g_")
    ]
    for key in keys_to_del:
        del st.session_state[key]


def parse_seed_text(seed_text):
    seeds = [int(s) for s in re.findall(r"\d+", seed_text)]
    if not seeds:
        return [123, 42, 2026]

    unique_seeds = []
    for seed in seeds:
        if seed not in unique_seeds:
            unique_seeds.append(seed)
    return unique_seeds[:6]


def figure_to_pdf_bytes(fig):
    buffer = io.BytesIO()
    fig.savefig(buffer, format="pdf", dpi=300, bbox_inches="tight", facecolor="white")
    buffer.seek(0)
    return buffer


def build_excel_report(final_agg, seed_results, combined_summary, top_k, toxic_cols):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        final_agg.to_excel(writer, sheet_name="ToxPi_Results", index=False)

        if combined_summary is not None:
            combined_summary.to_excel(writer, sheet_name="Sensitivity_Combined", index=False)

        metadata_rows = []
        for item in seed_results:
            seed = item["seed"]
            summary = item["summary"]
            stats = item["stats"]
            summary.to_excel(writer, sheet_name=f"Seed_{seed}"[:31], index=False)
            metadata_rows.append(
                {
                    "seed": seed,
                    "iterations": 1000,
                    "top_k": top_k,
                    "mean_rho": stats["mean"],
                    "sd": stats["sd"],
                    "ci_lower": stats["ci_lower"],
                    "ci_upper": stats["ci_upper"],
                }
            )

        pd.DataFrame(metadata_rows).to_excel(writer, sheet_name="Simulation_Metadata", index=False)
        pd.DataFrame({"toxicity_indicator": toxic_cols}).to_excel(
            writer, sheet_name="Toxicity_Indicators", index=False
        )

    buffer.seek(0)
    return buffer


def combine_seed_summaries(seed_results, top_k):
    if not seed_results:
        return None

    freq_col = f"top_{top_k}_frequency_percent"
    frames = []
    for item in seed_results:
        seed = item["seed"]
        summary = item["summary"][["compound", "toxpi", freq_col]].copy()
        summary = summary.rename(columns={freq_col: f"seed_{seed}_top_k_percent"})
        frames.append(summary)

    combined = frames[0]
    for frame in frames[1:]:
        combined = combined.merge(frame.drop(columns=["toxpi"]), on="compound", how="outer")

    seed_cols = [col for col in combined.columns if col.startswith("seed_")]
    combined["top_k_frequency_mean"] = combined[seed_cols].mean(axis=1).round(2)
    combined["top_k_frequency_min"] = combined[seed_cols].min(axis=1).round(2)
    combined["top_k_frequency_max"] = combined[seed_cols].max(axis=1).round(2)
    return combined.sort_values(by="toxpi", ascending=False).reset_index(drop=True)


st.title("🧬 ToxPi 毒性评估与排序稳健性分析")
st.caption("上传 Excel 后在线计算、预览图表，并通过下载按钮获取 PDF 或 Excel 结果。")
st.markdown("---")

st.sidebar.header("ToxPi 控制台")

uploaded_file = st.sidebar.file_uploader(
    "1. 上传污染物原始数据 (Excel)",
    type=["xlsx", "xls"],
    help="表格必须包含 compound 列，并至少包含 1 个可转为数字的毒性指标列；系统会按实际列自动调整。",
)

if uploaded_file is not None:
    if st.session_state.get("cached_filename") != uploaded_file.name:
        clear_cached_data()
    try:
        st.session_state["cached_df"] = load_and_clean_data(uploaded_file)
        st.session_state["cached_filename"] = uploaded_file.name
    except Exception as exc:
        clear_cached_data()
        st.error(f"上传文件解析失败：{exc}")

if "cached_df" in st.session_state:
    st.sidebar.success(f"已加载数据：{st.session_state['cached_filename']}")
    if st.sidebar.button("清空当前数据"):
        clear_cached_data()
        st.rerun()

if "cached_df" not in st.session_state:
    st.info("请先在左侧上传 Excel 数据文件。表格需要包含 `compound` 列，以及至少 1 个数值型毒性指标列。")
    st.stop()

cleaned_df = st.session_state["cached_df"]
candidate_toxic_cols = get_toxic_cols_from_frame(cleaned_df)
if not candidate_toxic_cols:
    st.error("没有识别到可用于 ToxPi 的数值型毒性指标列。")
    st.stop()

st.sidebar.markdown("---")
st.sidebar.markdown(f"**2. 选择本次参与计算的毒性指标**")
selection_key = f"selected_toxic_cols_{st.session_state['cached_filename']}"
if selection_key not in st.session_state:
    st.session_state[selection_key] = candidate_toxic_cols

toxic_cols = st.sidebar.multiselect(
    "选择要纳入本次 ToxPi 计算的指标",
    options=candidate_toxic_cols,
    default=[col for col in st.session_state[selection_key] if col in candidate_toxic_cols],
    help="默认选中所有识别到的数值型毒性列；你可以取消不想纳入本次计算的指标。",
)
st.session_state[selection_key] = toxic_cols

if not toxic_cols:
    st.error("请至少选择 1 个毒性指标参与 ToxPi 计算。")
    st.stop()

with st.sidebar.expander("查看可选毒性指标", expanded=False):
    st.write("系统从 Excel 中识别到下列可转为数字的候选指标列：")
    st.dataframe(pd.DataFrame({"candidate_toxicity_indicator": candidate_toxic_cols}), use_container_width=True)

st.sidebar.markdown("---")
st.sidebar.markdown(f"**3. 毒性因子权重（{len(toxic_cols)} 个指标）**")
with st.sidebar.expander("查看本次参与计算的毒性指标", expanded=False):
    st.write("系统会使用下列已选择的指标参与 ToxPi 计算：")
    st.dataframe(pd.DataFrame({"toxicity_indicator": toxic_cols}), use_container_width=True)

user_weights = {}
for col in toxic_cols:
    state_key = f"saved_weight_{col}"
    default_val = 2.0 if col == "carcinogenicity" else 1.0
    if state_key not in st.session_state:
        st.session_state[state_key] = default_val

    value = st.sidebar.slider(
        f"指标: {col}",
        min_value=0.0,
        max_value=10.0,
        value=st.session_state[state_key],
        step=0.5,
    )
    st.session_state[state_key] = value
    user_weights[col] = value

weight_total = sum(user_weights.values())

st.sidebar.markdown("---")
if "saved_top_k" not in st.session_state:
    st.session_state["saved_top_k"] = 3
user_top_k = st.sidebar.slider(
    "4. 稳健频次统计阈值 (Top K)",
    min_value=1,
    max_value=50,
    value=st.session_state["saved_top_k"],
)
st.session_state["saved_top_k"] = user_top_k

st.sidebar.markdown("---")
if "saved_seed_text" not in st.session_state:
    st.session_state["saved_seed_text"] = "123, 42, 2026"
seed_text_input = st.sidebar.text_input(
    "5. 蒙特卡洛随机种子列表",
    value=st.session_state["saved_seed_text"],
    help="可输入多个整数，例如：123, 42, 2026。最多使用前 6 个不同种子。",
)
st.session_state["saved_seed_text"] = seed_text_input
test_seeds = parse_seed_text(seed_text_input)

if weight_total <= 0:
    st.error("所有权重之和为 0。请至少为一个毒性指标设置大于 0 的权重。")
    st.stop()

try:
    normalized_df = safe_normalize_data(cleaned_df, toxic_cols=toxic_cols)
    final_agg = calculate_toxpi(normalized_df, custom_weights=user_weights, toxic_cols=toxic_cols)
except Exception as exc:
    st.error(f"数据计算失败：{exc}")
    st.stop()

compounds_list = final_agg["compound"].dropna().unique()
if len(compounds_list) == 0:
    st.error("没有可用于计算的化合物数据。")
    st.stop()

comp_to_group_map = {}
chosen_bar_colors = {}

st.sidebar.markdown("---")
with st.sidebar.expander("6. 种类划定与分组配色", expanded=False):
    st.caption("组名一致的化合物会使用同一种柱状图颜色。")

    for idx, comp_name in enumerate(compounds_list):
        group_state_key = f"group_comp_{comp_name}"
        if group_state_key not in st.session_state:
            st.session_state[group_state_key] = f"种类 {idx % 2 + 1}"

        group_name = st.text_input(
            f"{comp_name} 归属种类",
            value=st.session_state[group_state_key],
            key=f"text_in_{comp_name}",
        ).strip()
        group_name = group_name or "未分组"
        st.session_state[group_state_key] = group_name
        comp_to_group_map[comp_name] = group_name

    st.markdown("---")
    preset_group_colors = ["#4682B4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#bcbd22"]
    group_color_map = {}
    for idx, group_name in enumerate(sorted(set(comp_to_group_map.values()))):
        color_state_key = f"color_group_{group_name}"
        if color_state_key not in st.session_state:
            st.session_state[color_state_key] = preset_group_colors[idx % len(preset_group_colors)]

        picked_color = st.color_picker(
            f"{group_name} 颜色",
            value=st.session_state[color_state_key],
            key=f"picker_g_{group_name}",
        )
        st.session_state[color_state_key] = picked_color
        group_color_map[group_name] = picked_color

    for comp_name, group_name in comp_to_group_map.items():
        chosen_bar_colors[comp_name] = group_color_map[group_name]

tab1, tab2, tab3 = st.tabs(["数据审查", "ToxPi 图谱", "排序稳健性"])

with tab1:
    st.subheader("本次参与计算的毒性指标")
    st.dataframe(
        pd.DataFrame(
            {
                "toxicity_indicator": toxic_cols,
                "weight": [user_weights[col] for col in toxic_cols],
            }
        ),
        use_container_width=True,
    )

    st.subheader("原始数据")
    st.dataframe(cleaned_df, use_container_width=True)

    st.subheader("归一化数据")
    st.dataframe(normalized_df, use_container_width=True)

    st.subheader("ToxPi 得分")
    st.dataframe(final_agg[["compound", "toxpi"]], use_container_width=True)

with tab2:
    st.subheader("ToxPi 风玫瑰图")
    fig_rose_beautified = generate_multi_toxpi_plot(
        final_agg,
        custom_weights=user_weights,
        beautify=True,
        toxic_cols=toxic_cols,
    )
    st.pyplot(fig_rose_beautified)
    rose_beautified_pdf = figure_to_pdf_bytes(fig_rose_beautified)

    fig_rose_original = generate_multi_toxpi_plot(
        final_agg,
        custom_weights=user_weights,
        beautify=False,
        toxic_cols=toxic_cols,
    )
    rose_original_pdf = figure_to_pdf_bytes(fig_rose_original)

    col_a, col_b = st.columns(2)
    with col_a:
        st.download_button(
            label="下载美化版风玫瑰图 PDF",
            data=rose_beautified_pdf,
            file_name="ToxPi_Plot_Beautified.pdf",
            mime="application/pdf",
        )
    with col_b:
        st.download_button(
            label="下载原始版风玫瑰图 PDF",
            data=rose_original_pdf,
            file_name="ToxPi_Plot_Original.pdf",
            mime="application/pdf",
        )

    plt.close(fig_rose_beautified)
    plt.close(fig_rose_original)

    st.markdown("---")
    st.subheader("ToxPi 综合得分柱状图")
    fig_bar = generate_toxpi_bar_plot(final_agg, bar_colors_dict=chosen_bar_colors)
    st.pyplot(fig_bar)
    bar_pdf = figure_to_pdf_bytes(fig_bar)
    plt.close(fig_bar)

    st.download_button(
        label="下载柱状图 PDF",
        data=bar_pdf,
        file_name="ToxPi_Bar_Plot_Group_Colors.pdf",
        mime="application/pdf",
    )

with tab3:
    st.subheader("蒙特卡洛权重扰动分析")

    if len(compounds_list) < 2:
        st.warning("当前少于 2 个化合物，无法进行排序稳健性分析。")
    else:
        actual_top_k = min(user_top_k, len(compounds_list))
        if len(compounds_list) < 3:
            st.warning("当前样本量较小，Spearman 排序一致性仅供参考。")

        seed_results = []
        columns = st.columns(len(test_seeds))
        for idx, seed in enumerate(test_seeds):
            summary, stats, fig_cor, final_top_k = run_sensitivity_analysis(
                final_agg,
                custom_weights=user_weights,
                toxic_cols=toxic_cols,
                top_k=actual_top_k,
                seed=seed,
            )
            seed_results.append({"seed": seed, "summary": summary, "stats": stats})

            with columns[idx]:
                st.pyplot(fig_cor)
                cor_pdf = figure_to_pdf_bytes(fig_cor)
                st.download_button(
                    label=f"下载 seed {seed} 直方图",
                    data=cor_pdf,
                    file_name=f"Sensitivity_Distribution_seed_{seed}.pdf",
                    mime="application/pdf",
                    key=f"dl_btn_seed_{seed}",
                )
                st.metric("Mean Rho", f"{stats['mean']:.3f}")
            plt.close(fig_cor)

        combined_summary = combine_seed_summaries(seed_results, actual_top_k)

        st.markdown("---")
        st.subheader(f"多 seed 汇总表 (Top {actual_top_k})")
        st.dataframe(combined_summary, use_container_width=True)

        excel_buffer = build_excel_report(final_agg, seed_results, combined_summary, actual_top_k, toxic_cols)
        st.download_button(
            label="下载完整计算报告 Excel",
            data=excel_buffer,
            file_name="ToxPi_Calculated_Report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
