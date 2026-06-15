import io

import pandas as pd
import streamlit as st


st.set_page_config(
    page_title="ADMETlab 毒性数据获取 - ChemPriority",
    page_icon="🧪",
    layout="wide",
)


REQUIRED_COLUMNS = ["compound", "smiles"]


def make_template_file():
    template_df = pd.DataFrame(
        {
            "compound": ["example_compound_1", "example_compound_2"],
            "smiles": ["CCO", "c1ccccc1"],
        }
    )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        template_df.to_excel(writer, sheet_name="ADMETlab_Input", index=False)
    buffer.seek(0)
    return buffer


def normalize_input_columns(df):
    normalized = df.copy()
    normalized.columns = [str(col).strip() for col in normalized.columns]

    rename_map = {}
    for col in normalized.columns:
        lower_col = col.lower()
        if lower_col in {"compound", "name", "compound_name", "chemical", "chemical_name"}:
            rename_map[col] = "compound"
        elif lower_col in {"smiles", "canonical_smiles", "isomeric_smiles"}:
            rename_map[col] = "smiles"

    normalized = normalized.rename(columns=rename_map)
    return normalized


def validate_input(df):
    missing_cols = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing_cols:
        return False, f"缺少必要列：{', '.join(missing_cols)}"

    empty_rows = df[REQUIRED_COLUMNS].isna().any(axis=1).sum()
    if empty_rows > 0:
        return False, f"compound 或 smiles 存在空值，请先处理 {empty_rows} 行不完整数据。"

    duplicated = df["compound"].duplicated().sum()
    if duplicated > 0:
        return False, f"compound 存在 {duplicated} 个重复名称，请先确认是否需要合并或重命名。"

    return True, "输入数据检查通过。"


st.title("🧪 ADMETlab 毒性数据获取")
st.caption("上传 compound + smiles 表格，后续用于自动连接 ADMETlab 平台并下载毒性预测结果。")
st.markdown("---")

left_col, right_col = st.columns([2, 1])

with left_col:
    st.subheader("1. 上传 ADMETlab 输入表")
    uploaded_file = st.file_uploader(
        "上传 Excel 文件",
        type=["xlsx", "xls"],
        help="文件至少需要包含 compound 和 smiles 两列。",
    )

with right_col:
    st.subheader("输入模板")
    st.download_button(
        label="下载 Excel 模板",
        data=make_template_file(),
        file_name="ADMETlab_Input_Template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

if uploaded_file is None:
    st.info("请先上传包含 compound 和 smiles 的 Excel 文件。")
    st.stop()

try:
    input_df = pd.read_excel(uploaded_file)
    input_df = normalize_input_columns(input_df)
except Exception as exc:
    st.error(f"Excel 读取失败：{exc}")
    st.stop()

is_valid, message = validate_input(input_df)
if not is_valid:
    st.error(message)
    st.dataframe(input_df, use_container_width=True)
    st.stop()

st.success(message)

tab_input, tab_connector, tab_output = st.tabs(["输入数据", "ADMETlab 连接", "结果下载"])

with tab_input:
    st.subheader("待提交化合物")
    st.dataframe(input_df[REQUIRED_COLUMNS], use_container_width=True)
    st.metric("化合物数量", len(input_df))

with tab_connector:
    st.subheader("连接状态")
    st.warning(
        "ADMETlab 自动连接尚未启用。下一步需要根据 ADMETlab 官方接口或批量任务流程实现提交、轮询和结果解析。"
    )
    st.write("计划流程：")
    st.markdown(
        "\n".join(
            [
                "1. 将 compound + smiles 批量提交到 ADMETlab。",
                "2. 获取并保存 ADMETlab 原始预测结果。",
                "3. 根据字段映射提取 ToxPi 所需毒性指标。",
                "4. 允许用户下载原始结果和 ToxPi 输入格式结果。",
            ]
        )
    )
    st.link_button("打开 ADMETlab 平台", "https://admetlab3.scbdd.com/")

with tab_output:
    st.subheader("当前可下载内容")
    st.write("在 ADMETlab 接口完成前，当前页面先提供已校验输入数据的下载，便于确认批量提交清单。")

    output_buffer = io.BytesIO()
    with pd.ExcelWriter(output_buffer, engine="openpyxl") as writer:
        input_df[REQUIRED_COLUMNS].to_excel(writer, sheet_name="Validated_Input", index=False)
    output_buffer.seek(0)

    st.download_button(
        label="下载已校验输入表",
        data=output_buffer,
        file_name="ADMETlab_Validated_Input.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
