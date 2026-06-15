import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from scipy.stats import spearmanr

# 1. 全局绘图环境配置（支持可编辑矢量图，防止中文乱码）
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial']
plt.rcParams['axes.unicode_minus'] = False
plt.rcParams['pdf.fonttype'] = 42

# 标准的 15 种毒性指标列名
TOXIC_COLS = [
    "carcinogenicity", "A549", "DILI", "RPMI", "eye_irritation",
    "genotoxicity", "hematotoxicity", "hERG", "liver_toxicity", "nephrotoxicity",
    "neurotoxicity", "oral_cavity", "ototoxicity", "respiratory_toxicity", "skin_sensitivity"
]

# 严格还原 PDF 效果的 15 种 HEX 颜色字典
METRIC_COLORS = {
    "carcinogenicity": "#d62728", "A549": "#1f77b4", "DILI": "#ff7f0e",
    "RPMI": "#2ca02c", "eye_irritation": "#9467bd", "genotoxicity": "#8c564b",
    "hematotoxicity": "#e377c2", "hERG": "#7f7f7f", "liver_toxicity": "#bcbd22",
    "nephrotoxicity": "#17becf", "neurotoxicity": "#aec7e8", "oral_cavity": "#ffbb78",
    "ototoxicity": "#98df8a", "respiratory_toxicity": "#ff9896", "skin_sensitivity": "#c5b0d5"
}


def get_default_weights():
    """获取默认权重分配：致癌性2/16，其余 1/16"""
    weights = {col: 1 / 16 for col in TOXIC_COLS}
    weights["carcinogenicity"] = 2 / 16
    return weights


def normalize_weights(custom_weights=None):
    """校验并归一化权重，避免全 0 或缺失权重进入计算。"""
    if custom_weights is None:
        custom_weights = get_default_weights()

    weights = {col: float(custom_weights.get(col, 0)) for col in TOXIC_COLS}
    total_w = sum(weights.values())
    if total_w <= 0:
        raise ValueError("所有毒性指标权重之和必须大于 0。")
    return {col: weights[col] / total_w for col in TOXIC_COLS}


def score_indicator_matrix(indicator_matrix, weights):
    """按每行非缺失指标重新分配权重，计算 ToxPi 分数。"""
    weight_vector = np.array([weights[col] for col in TOXIC_COLS])
    non_na_mask = ~np.isnan(indicator_matrix)
    clean_matrix = np.nan_to_num(indicator_matrix, nan=0.0)
    scores = np.full(indicator_matrix.shape[0], np.nan)

    for row_idx in range(indicator_matrix.shape[0]):
        valid_w = weight_vector * non_na_mask[row_idx]
        if valid_w.sum() > 0:
            valid_w = valid_w / valid_w.sum()
            scores[row_idx] = np.sum(clean_matrix[row_idx] * valid_w)
    return scores


def load_and_clean_data(file_path_or_buffer, sheet_name=0):
    """
    第一部分：读取并清洗数据模块
    """
    try:
        df = pd.read_excel(file_path_or_buffer, sheet_name=sheet_name)
    except Exception:
        df = pd.read_excel(file_path_or_buffer)

    df.columns = [str(c).strip() for c in df.columns]

    rename_dict = {
        "hERG Blockers-hERG": "hERG", "hERG Blockers": "hERG",
        "oral cavity": "oral_cavity", "Skin sensitivity": "skin_sensitivity",
        "Eye irritation": "eye_irritation", "Respiratory toxicity": "respiratory_toxicity",
        "liver toxicity": "liver_toxicity", "RPMI-8226": "RPMI"
    }
    df = df.rename(columns=rename_dict)

    missing_cols = [col for col in TOXIC_COLS if col not in df.columns]
    if "compound" not in df.columns:
        missing_cols.append("compound")

    if missing_cols:
        raise ValueError(f"数据表格中缺少必要列：{', '.join(missing_cols)}")

    for col in TOXIC_COLS:
        df[col] = df[col].replace("NA", np.nan)
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df.dropna(subset=TOXIC_COLS, how='all')
    return df.reset_index(drop=True)


def safe_normalize_data(df):
    """
    第二部分：基于 5%-95% 分位数的归一化模块
    """
    norm_df = df.copy()
    for col in TOXIC_COLS:
        valid_data = norm_df[col].dropna()
        if len(valid_data) == 0:
            norm_df[f"norm_{col}"] = np.nan
            continue

        q05 = np.percentile(valid_data, 5)
        q95 = np.percentile(valid_data, 95)

        if q95 == q05:
            vmin, vmax = valid_data.min(), valid_data.max()
            if vmax == vmin:
                scaled = np.zeros_like(norm_df[col])
            else:
                scaled = (norm_df[col] - vmin) / (vmax - vmin)
        else:
            scaled = (norm_df[col] - q05) / (q95 - q05)

        norm_df[f"norm_{col}"] = np.clip(scaled, 0, 1)
    return norm_df


def calculate_toxpi(norm_df, custom_weights=None):
    """
    第三部分：核心 ToxPi 算分与平均值合并模块
    """
    norm_weights = normalize_weights(custom_weights)

    norm_cols = [f"norm_{col}" for col in TOXIC_COLS]
    matrix_data = norm_df[norm_cols].values
    toxpi_scores = score_indicator_matrix(matrix_data, norm_weights)

    result_df = norm_df.copy()
    result_df["toxpi"] = toxpi_scores

    agg_dict = {f"norm_{col}": "mean" for col in TOXIC_COLS}
    agg_dict["toxpi"] = "mean"

    toxpi_agg = result_df.groupby("compound", as_index=False).agg(agg_dict)
    toxpi_agg = toxpi_agg.sort_values(by="toxpi", ascending=False).reset_index(drop=True)
    return toxpi_agg


def run_sensitivity_analysis(toxpi_agg, custom_weights=None, n_iter=1000, seed=123, top_k=3):
    """
    第四部分：1000次蒙特卡洛敏感性分析
    """
    norm_weights_dict = normalize_weights(custom_weights)

    norm_cols = [f"norm_{col}" for col in TOXIC_COLS]
    orig_weights = np.array([norm_weights_dict[col] for col in TOXIC_COLS])

    compounds = toxpi_agg["compound"].values
    indicator_matrix = toxpi_agg[norm_cols].values
    non_na_mask = ~np.isnan(indicator_matrix)
    clean_matrix = np.nan_to_num(indicator_matrix, nan=0.0)

    if len(compounds) < 2:
        raise ValueError("至少需要 2 个化合物才能进行排序稳健性分析。")

    actual_top_k = min(top_k, len(compounds))

    np.random.seed(seed)

    orig_order = np.argsort(-toxpi_agg["toxpi"].values)
    orig_ranks = np.argsort(orig_order) + 1

    cor_sp = np.zeros(n_iter)
    top_k_counts = {c: 0 for c in compounds}

    perturbed_weights = np.random.uniform(0.8, 1.2, size=(n_iter, len(orig_weights))) * orig_weights
    perturbed_weights = perturbed_weights / perturbed_weights.sum(axis=1, keepdims=True)

    for i in range(n_iter):
        w = perturbed_weights[i]
        scores = np.full(len(compounds), np.nan)

        for c_idx in range(len(compounds)):
            valid_w = w * non_na_mask[c_idx]
            if valid_w.sum() > 0:
                valid_w = valid_w / valid_w.sum()
                scores[c_idx] = np.sum(clean_matrix[c_idx] * valid_w)

        new_order = np.argsort(-np.nan_to_num(scores, nan=-np.inf))

        top_k_this = compounds[new_order[:actual_top_k]]
        for c in top_k_this:
            if c in top_k_counts:
                top_k_counts[c] += 1

        new_ranks = np.argsort(new_order) + 1
        rho, _ = spearmanr(orig_ranks, new_ranks)
        cor_sp[i] = 0.0 if np.isnan(rho) else rho

    freq_col_name = f"top_{actual_top_k}_frequency_percent"

    summary_df = pd.DataFrame({
        "compound": compounds,
        "toxpi": toxpi_agg["toxpi"].values,
        freq_col_name: [round((top_k_counts[c] / n_iter) * 100, 2) for c in compounds]
    }).sort_values(by="toxpi", ascending=False).reset_index(drop=True)

    stats = {
        "mean": round(np.mean(cor_sp), 3),
        "sd": round(np.std(cor_sp), 3),
        "ci_lower": round(np.percentile(cor_sp, 2.5), 3),
        "ci_upper": round(np.percentile(cor_sp, 97.5), 3)
    }

    fig_cor, ax = plt.subplots(figsize=(7, 5.2))
    ax.hist(cor_sp, bins=30, color="darkgreen", alpha=0.7, edgecolor="black", linewidth=0.5, zorder=3)

    mean_val = stats["mean"]
    ax.axvline(mean_val, color="red", linestyle="--", linewidth=1.2, zorder=4)

    ax.set_title(f"权重随机扰动下排序一致性分布 (seed={seed})", fontsize=12, pad=15, color='#111111')
    ax.set_xlabel("Spearman 相关系数（与原始排序）", fontsize=11, labelpad=8, color='#111111')
    ax.set_ylabel("频次", fontsize=11, labelpad=8, color='#111111')

    ax.set_ylim(0, 1000)

    for spine in ['top', 'right', 'left', 'bottom']:
        ax.spines[spine].set_visible(False)

    ax.grid(True, linestyle='-', color='#E5E5E5', linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(axis='both', which='both', length=0, labelsize=10, colors='#333333')

    offset_x = 0.01 if mean_val < 0.95 else -0.06
    ax.text(mean_val + offset_x, 800, f"均值 = {mean_val:.3f}", color="red", fontsize=11)

    return summary_df, stats, fig_cor, actual_top_k


def generate_multi_toxpi_plot(toxpi_agg, custom_weights=None, beautify=True):
    """
    第五部分：完整的发表级静态矢量风玫瑰图生成芯片
    """
    norm_weights_dict = normalize_weights(custom_weights)

    widths = np.array([norm_weights_dict[col] * 2 * np.pi for col in TOXIC_COLS])
    starts = np.zeros(len(TOXIC_COLS))
    for i in range(1, len(TOXIC_COLS)):
        starts[i] = starts[i - 1] + widths[i - 1]

    n_compounds = len(toxpi_agg)
    n_cols = min(4, n_compounds)
    n_rows = int(np.ceil(n_compounds / n_cols))

    fig, axes = plt.subplots(n_rows, n_cols, subplot_kw={'projection': 'polar'}, figsize=(4 * n_cols, 6.5 * n_rows))
    if n_compounds == 1:
        axes = np.array([axes])
    axes = axes.flatten()

    plt.subplots_adjust(left=0.06, right=0.94, top=0.78, bottom=0.30, wspace=0.5)
    norm_cols = [f"norm_{col}" for col in TOXIC_COLS]

    for idx in range(len(axes)):
        ax = axes[idx]
        if idx >= n_compounds:
            ax.axis('off')
            continue

        row_data = toxpi_agg.iloc[idx]
        name = row_data["compound"]
        score = row_data["toxpi"]

        scores_vector = np.nan_to_num(row_data[norm_cols].values.astype(float), nan=0.0)

        if beautify:
            MIN_THICKNESS = 0.05
            scores_vector = np.where(scores_vector == 0.0, MIN_THICKNESS, scores_vector)

        ax.set_theta_zero_location("N")
        ax.set_theta_direction(-1)

        ax.bar(
            starts, scores_vector, width=widths, bottom=0.2,
            color=[METRIC_COLORS[m] for m in TOXIC_COLS],
            edgecolor='white', linewidth=0.4, align='edge'
        )
        ax.set_ylim(0, 1.2)
        ax.axis('off')

        ax.text(0.5, -0.15, name, fontsize=14, fontweight='bold', ha='center', va='top', transform=ax.transAxes)
        ax.text(0.5, -0.35, f"ToxPi: {score:.2f}", fontsize=11, color='black', ha='center', va='top',
                transform=ax.transAxes)

    layout_str = "Beautified Layout" if beautify else "Strict Original Layout"
    fig.suptitle(
        f"ToxPi Scores for All Pollutants ({n_compounds} compounds) - {layout_str}",
        fontsize=16, y=0.92, ha='center'
    )

    col_major_indices = [0, 3, 6, 9, 12, 1, 4, 7, 10, 13, 2, 5, 8, 11, 14]
    legend_elements = [Patch(facecolor=METRIC_COLORS[TOXIC_COLS[i]], label=TOXIC_COLS[i]) for i in col_major_indices]

    fig.legend(
        handles=legend_elements, title="Toxicity Factor", title_fontsize=12,
        loc="lower center", bbox_to_anchor=(0.5, 0.08), ncol=5, fontsize=11,
        frameon=False, labelspacing=0.5, handletextpad=0.5, columnspacing=2.0
    )

    caption_text = "Radial length = Normalized value | Weights normalized to 100%"
    if beautify:
        caption_text += " | Zero values use minimum visual thickness"
    fig.text(0.94, 0.04, caption_text, fontsize=9.5, color='#333333', ha='right', va='bottom')
    return fig


def generate_toxpi_bar_plot(toxpi_agg, bar_colors_dict=None):
    """
    第六部分：支持多色按种类批量映射的自适应柱状图芯片
    """
    compounds = toxpi_agg["compound"].values
    scores = toxpi_agg["toxpi"].values

    # 动态把种类分配的最终色泽序列按降序编译出来
    colors_list = []
    for comp in compounds:
        if bar_colors_dict and comp in bar_colors_dict:
            colors_list.append(bar_colors_dict[comp])
        else:
            colors_list.append("#4682B4")  # 钢蓝色兜底

    fig, ax = plt.subplots(figsize=(6.5, 4.6))

    bars = ax.bar(
        compounds, scores, color=colors_list, edgecolor="black",
        linewidth=0.8, width=0.42, alpha=0.9, zorder=3
    )

    ax.set_title("各污染物 ToxPi 综合毒性得分对比图", fontsize=12, pad=15, fontweight='bold', color='#000000')
    ax.set_ylabel("ToxPi Score", fontsize=11, labelpad=8, color='#000000')

    ax.set_ymargin(0.15)
    ax.set_xlabel("")

    for bar in bars:
        height = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2., height + 0.015, f"{height:.2f}",
            ha='center', va='bottom', fontsize=10, fontweight='bold', color='#111111'
        )

    ax.grid(False)
    for spine in ['top', 'right', 'left', 'bottom']:
        ax.spines[spine].set_visible(True)
        ax.spines[spine].set_color('black')
        ax.spines[spine].set_linewidth(1.0)

    ax.tick_params(axis='both', which='both', direction='in', length=4, labelsize=10, colors='#000000')

    return fig


def export_results_to_excel(toxpi_agg, sensitivity_df, stats_dict, excel_path, top_k_value):
    """
    第六部分：一键导出所有计算结果到本地 Excel 表格
    """
    metadata_df = pd.DataFrame({
        "统计指标说明": [
            "蒙特卡洛模拟总次数 (Iterations)",
            f"排序频次统计阈值 (筛选前 Top {top_k_value} 名)",
            "Spearman相关系数均值 (Mean Rho)",
            "排名扰动标准差 (Standard Deviation)",
            "95% 置信区间下限 (2.5th Percentile)",
            "95% 置信区间上限 (97.5th Percentile)"
        ],
        "对应数值": [
            1000,
            top_k_value,
            stats_dict["mean"],
            stats_dict["sd"],
            stats_dict["ci_lower"],
            stats_dict["ci_upper"]
        ]
    })

    with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
        toxpi_agg.to_excel(writer, sheet_name='ToxPi_Results', index=False)
        sensitivity_df.to_excel(writer, sheet_name='Sensitivity_Summary', index=False)
        metadata_df.to_excel(writer, sheet_name='Simulation_Metadata', index=False)
