import streamlit as st


st.set_page_config(
    page_title="ToxApp 综合环境毒性评估平台",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("ToxApp 综合环境毒性评估平台")
st.caption("面向污染物优先控制筛选的数据获取、用途识别、ToxPi 计算与环境归趋预测工具。")
st.markdown("---")

entry_tab, data_tab, note_tab = st.tabs(["功能入口", "数据格式", "部署说明"])

with entry_tab:
    st.subheader("四个独立功能模块")
    col_admet, col_toxpi, col_epi, col_use = st.columns(4)

    with col_admet:
        st.markdown("### 1. ADMETlab 毒性数据获取")
        st.write(
            "上传包含化合物名称和 SMILES 的 Excel 文件。ADMETlab 自动连接路线暂不启用，"
            "当前页面用于整理和校验后续批量提交清单。"
        )
        st.info("从左侧第一个页面进入“ADMETlab毒性数据获取”。")

    with col_toxpi:
        st.markdown("### 2. ToxPi 毒性评估")
        st.write(
            "保持原有 ToxPi 功能不变：上传毒性指标数据后完成归一化、加权评分、"
            "图表生成和排序稳定性分析。"
        )
        st.info("从左侧第二个页面进入“🧬 ToxPi毒性评估”。")

    with col_epi:
        st.markdown("### 3. EPI Suite 环境归趋")
        st.write(
            "通过 EPI Web Suite 网页端 API 计算物化性质、降解、生物富集和环境介质分配等指标。"
        )
        st.info("从左侧第三个页面进入“EPISuite环境归趋”。")

    with col_use:
        st.markdown("### 4. EPA/ECHA 用途查询")
        st.write(
            "上传化合物表格，连接 EPA CompTox Dashboard 和 ECHA CHEM 查询用途证据，"
            "可先补全 CAS、DTXSID、EC 和 ECHA ID，再按证据强度排序并提取前五个用途。"
        )
        st.info("从左侧第四个页面进入“化合物用途查询”。")

    st.markdown("---")
    st.metric("当前已隔离模块", "4 个")

with data_tab:
    st.subheader("ADMETlab 输入表格")
    st.write("Excel 文件建议包含以下两列。")
    st.code("compound\nsmiles", language="text")

    st.subheader("ToxPi 输入表格")
    st.write("Excel 文件需要包含一列 `compound`，以及以下 15 个毒性指标列。")
    st.code(
        "\n".join(
            [
                "carcinogenicity",
                "A549",
                "DILI",
                "RPMI",
                "eye_irritation",
                "genotoxicity",
                "hematotoxicity",
                "hERG",
                "liver_toxicity",
                "nephrotoxicity",
                "neurotoxicity",
                "oral_cavity",
                "ototoxicity",
                "respiratory_toxicity",
                "skin_sensitivity",
            ]
        ),
        language="text",
    )
    st.write(
        "系统也兼容部分常见原始列名，例如 `hERG Blockers-hERG`、`oral cavity`、"
        "`Skin sensitivity`、`RPMI-8226` 等。"
    )

    st.subheader("EPI Suite 输入表格")
    st.write("建议复用 `compound` 和 `smiles` 两列。")

    st.subheader("EPA/ECHA 用途查询输入表格")
    st.write("只有 `smiles` 时可以先做标识符补全；EPA 建议包含 `compound`、`cas`、`smiles`、`dtxsid`；ECHA 建议包含 `compound`、`ec`、`cas`、`smiles`、`echa_id`。")
    st.code("compound\ncas\nec\nsmiles\ndtxsid\necha_id", language="text")

with note_tab:
    st.subheader("线上使用方式")
    st.write(
        "本应用按 Streamlit 网页部署设计。四个模块保持页面隔离：ADMETlab 数据整理、ToxPi 算分、"
        "EPI Suite 环境归趋预测、EPA/ECHA 用途查询分别维护，后续扩展时不需要改动原有 ToxPi 页面。"
    )
    st.write("部署前建议使用项目根目录的 `requirements.txt` 安装依赖，并确认服务器可以访问 EPA 和 ECHA 相关网页及接口。")
