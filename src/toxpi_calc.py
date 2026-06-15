import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from scipy.stats import spearmanr


plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "Arial"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["pdf.fonttype"] = 42


# Legacy ADMETlab/ToxPi indicators. They are still used by old templates and
# tests, but uploaded files can now use any numeric toxicity indicator columns.
TOXIC_COLS = [
    "carcinogenicity", "A549", "DILI", "RPMI", "eye_irritation",
    "genotoxicity", "hematotoxicity", "hERG", "liver_toxicity", "nephrotoxicity",
    "neurotoxicity", "oral_cavity", "ototoxicity", "respiratory_toxicity", "skin_sensitivity",
]

METRIC_COLORS = {
    "carcinogenicity": "#d62728", "A549": "#1f77b4", "DILI": "#ff7f0e",
    "RPMI": "#2ca02c", "eye_irritation": "#9467bd", "genotoxicity": "#8c564b",
    "hematotoxicity": "#e377c2", "hERG": "#7f7f7f", "liver_toxicity": "#bcbd22",
    "nephrotoxicity": "#17becf", "neurotoxicity": "#aec7e8", "oral_cavity": "#ffbb78",
    "ototoxicity": "#98df8a", "respiratory_toxicity": "#ff9896", "skin_sensitivity": "#c5b0d5",
}

COLOR_PALETTE = [
    "#d62728", "#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5",
    "#1b9e77", "#d95f02", "#7570b3", "#e7298a", "#66a61e",
]

RENAME_DICT = {
    "hERG Blockers-hERG": "hERG",
    "hERG Blockers": "hERG",
    "oral cavity": "oral_cavity",
    "Skin sensitivity": "skin_sensitivity",
    "Eye irritation": "eye_irritation",
    "Respiratory toxicity": "respiratory_toxicity",
    "liver toxicity": "liver_toxicity",
    "RPMI-8226": "RPMI",
}

METADATA_COLUMNS = {
    "compound", "name", "chemical", "chemical_name", "compound_name",
    "cas", "casrn", "cas_no", "cas_number", "smiles", "canonical_smiles",
    "isomeric_smiles", "dtxsid", "ec", "echa_id", "formula", "molecular_formula",
    "molecular_weight", "category", "class", "group", "source", "notes", "note",
}


def infer_toxic_columns(df, candidate_columns=None):
    """Infer toxicity indicators from numeric columns in the uploaded table."""
    columns = list(candidate_columns) if candidate_columns is not None else list(df.columns)
    toxic_cols = []

    for col in columns:
        col_name = str(col).strip()
        if _normalize_key(col_name) in {_normalize_key(item) for item in METADATA_COLUMNS}:
            continue
        if col_name.startswith("norm_") or col_name == "toxpi":
            continue
        if col_name not in df.columns:
            continue

        numeric = _to_numeric_series(df[col_name])
        if numeric.notna().any():
            toxic_cols.append(col_name)

    return toxic_cols


def get_toxic_cols_from_frame(df, toxic_cols=None):
    if toxic_cols:
        return list(toxic_cols)
    if hasattr(df, "attrs") and df.attrs.get("toxic_cols"):
        return list(df.attrs["toxic_cols"])

    norm_cols = [col for col in df.columns if str(col).startswith("norm_")]
    if norm_cols:
        return [str(col)[5:] for col in norm_cols]

    return infer_toxic_columns(df)


def get_default_weights(toxic_cols=None):
    toxic_cols = list(toxic_cols or TOXIC_COLS)
    weights = {col: 1.0 for col in toxic_cols}
    if "carcinogenicity" in weights:
        weights["carcinogenicity"] = 2.0
    return weights


def normalize_weights(custom_weights=None, toxic_cols=None):
    if toxic_cols is None:
        toxic_cols = list(custom_weights.keys()) if custom_weights else list(TOXIC_COLS)
    toxic_cols = list(toxic_cols)
    if not toxic_cols:
        raise ValueError("没有可用于 ToxPi 计算的毒性指标列。")

    default_weights = get_default_weights(toxic_cols)
    custom_weights = custom_weights or default_weights
    weights = {col: float(custom_weights.get(col, default_weights.get(col, 1.0))) for col in toxic_cols}
    total_w = sum(weights.values())
    if total_w <= 0:
        raise ValueError("所有毒性指标权重之和必须大于 0。")
    return {col: weights[col] / total_w for col in toxic_cols}


def score_indicator_matrix(indicator_matrix, weights, toxic_cols=None):
    toxic_cols = list(toxic_cols or weights.keys())
    weight_vector = np.array([weights[col] for col in toxic_cols])
    non_na_mask = ~np.isnan(indicator_matrix)
    clean_matrix = np.nan_to_num(indicator_matrix, nan=0.0)
    scores = np.full(indicator_matrix.shape[0], np.nan)

    for row_idx in range(indicator_matrix.shape[0]):
        valid_w = weight_vector * non_na_mask[row_idx]
        if valid_w.sum() > 0:
            valid_w = valid_w / valid_w.sum()
            scores[row_idx] = np.sum(clean_matrix[row_idx] * valid_w)
    return scores


def load_and_clean_data(file_path_or_buffer, sheet_name=0, toxic_cols=None):
    try:
        df = pd.read_excel(file_path_or_buffer, sheet_name=sheet_name)
    except Exception:
        df = pd.read_excel(file_path_or_buffer)

    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns=RENAME_DICT)

    if "compound" not in df.columns:
        raise ValueError("数据表格中缺少必要列：compound")

    if toxic_cols is None:
        toxic_cols = infer_toxic_columns(df)
    else:
        toxic_cols = [str(col).strip() for col in toxic_cols]

    missing_cols = [col for col in toxic_cols if col not in df.columns]
    if missing_cols:
        raise ValueError(f"选择的毒性指标列不存在：{', '.join(missing_cols)}")

    if not toxic_cols:
        raise ValueError("没有识别到可用于 ToxPi 的数值型毒性指标列。请保留 compound 列，并至少提供一个数值型毒性指标列。")

    cleaned = df.copy()
    for col in toxic_cols:
        cleaned[col] = _to_numeric_series(cleaned[col])

    cleaned = cleaned.dropna(subset=toxic_cols, how="all")
    if cleaned.empty:
        raise ValueError("所有化合物的毒性指标均为空，无法计算 ToxPi。")

    cleaned = cleaned.reset_index(drop=True)
    cleaned.attrs["toxic_cols"] = toxic_cols
    return cleaned


def safe_normalize_data(df, toxic_cols=None):
    toxic_cols = get_toxic_cols_from_frame(df, toxic_cols)
    if not toxic_cols:
        raise ValueError("没有可归一化的毒性指标列。")

    norm_df = df.copy()
    for col in toxic_cols:
        valid_data = norm_df[col].dropna()
        if len(valid_data) == 0:
            norm_df[f"norm_{col}"] = np.nan
            continue

        q05 = np.percentile(valid_data, 5)
        q95 = np.percentile(valid_data, 95)

        if q95 == q05:
            vmin, vmax = valid_data.min(), valid_data.max()
            if vmax == vmin:
                scaled = np.zeros_like(norm_df[col], dtype=float)
            else:
                scaled = (norm_df[col] - vmin) / (vmax - vmin)
        else:
            scaled = (norm_df[col] - q05) / (q95 - q05)

        norm_df[f"norm_{col}"] = np.clip(scaled, 0, 1)

    norm_df.attrs["toxic_cols"] = toxic_cols
    return norm_df


def calculate_toxpi(norm_df, custom_weights=None, toxic_cols=None):
    toxic_cols = get_toxic_cols_from_frame(norm_df, toxic_cols)
    norm_weights = normalize_weights(custom_weights, toxic_cols=toxic_cols)

    norm_cols = [f"norm_{col}" for col in toxic_cols]
    missing_norm_cols = [col for col in norm_cols if col not in norm_df.columns]
    if missing_norm_cols:
        raise ValueError(f"缺少归一化列：{', '.join(missing_norm_cols)}")

    matrix_data = norm_df[norm_cols].values.astype(float)
    toxpi_scores = score_indicator_matrix(matrix_data, norm_weights, toxic_cols=toxic_cols)

    result_df = norm_df.copy()
    result_df["toxpi"] = toxpi_scores

    agg_dict = {f"norm_{col}": "mean" for col in toxic_cols}
    agg_dict["toxpi"] = "mean"

    toxpi_agg = result_df.groupby("compound", as_index=False).agg(agg_dict)
    toxpi_agg = toxpi_agg.sort_values(by="toxpi", ascending=False).reset_index(drop=True)
    toxpi_agg.attrs["toxic_cols"] = toxic_cols
    return toxpi_agg


def run_sensitivity_analysis(toxpi_agg, custom_weights=None, n_iter=1000, seed=123, top_k=3, toxic_cols=None):
    toxic_cols = get_toxic_cols_from_frame(toxpi_agg, toxic_cols)
    norm_weights_dict = normalize_weights(custom_weights, toxic_cols=toxic_cols)

    norm_cols = [f"norm_{col}" for col in toxic_cols]
    orig_weights = np.array([norm_weights_dict[col] for col in toxic_cols])

    compounds = toxpi_agg["compound"].values
    indicator_matrix = toxpi_agg[norm_cols].values.astype(float)
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
        freq_col_name: [round((top_k_counts[c] / n_iter) * 100, 2) for c in compounds],
    }).sort_values(by="toxpi", ascending=False).reset_index(drop=True)

    stats = {
        "mean": round(np.mean(cor_sp), 3),
        "sd": round(np.std(cor_sp), 3),
        "ci_lower": round(np.percentile(cor_sp, 2.5), 3),
        "ci_upper": round(np.percentile(cor_sp, 97.5), 3),
    }

    fig_cor, ax = plt.subplots(figsize=(7, 5.2))
    ax.hist(cor_sp, bins=30, color="darkgreen", alpha=0.7, edgecolor="black", linewidth=0.5, zorder=3)

    mean_val = stats["mean"]
    ax.axvline(mean_val, color="red", linestyle="--", linewidth=1.2, zorder=4)

    ax.set_title(f"权重随机扰动下排序一致性分布 (seed={seed})", fontsize=12, pad=15, color="#111111")
    ax.set_xlabel("Spearman 相关系数（与原始排序）", fontsize=11, labelpad=8, color="#111111")
    ax.set_ylabel("频次", fontsize=11, labelpad=8, color="#111111")

    ax.set_ylim(0, 1000)

    for spine in ["top", "right", "left", "bottom"]:
        ax.spines[spine].set_visible(False)

    ax.grid(True, linestyle="-", color="#E5E5E5", linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", which="both", length=0, labelsize=10, colors="#333333")

    offset_x = 0.01 if mean_val < 0.95 else -0.06
    ax.text(mean_val + offset_x, 800, f"均值 = {mean_val:.3f}", color="red", fontsize=11)

    return summary_df, stats, fig_cor, actual_top_k


def generate_multi_toxpi_plot(toxpi_agg, custom_weights=None, beautify=True, toxic_cols=None):
    toxic_cols = get_toxic_cols_from_frame(toxpi_agg, toxic_cols)
    norm_weights_dict = normalize_weights(custom_weights, toxic_cols=toxic_cols)

    widths = np.array([norm_weights_dict[col] * 2 * np.pi for col in toxic_cols])
    starts = np.zeros(len(toxic_cols))
    for i in range(1, len(toxic_cols)):
        starts[i] = starts[i - 1] + widths[i - 1]

    n_compounds = len(toxpi_agg)
    n_cols = min(4, n_compounds)
    n_rows = int(np.ceil(n_compounds / n_cols))

    fig_height = max(5.8, 5.8 * n_rows + min(len(toxic_cols), 20) * 0.08)
    fig, axes = plt.subplots(
        n_rows,
        n_cols,
        subplot_kw={"projection": "polar"},
        figsize=(4 * n_cols, fig_height),
    )
    if n_compounds == 1:
        axes = np.array([axes])
    axes = np.asarray(axes).flatten()

    plt.subplots_adjust(left=0.06, right=0.94, top=0.78, bottom=0.28, wspace=0.5)
    norm_cols = [f"norm_{col}" for col in toxic_cols]
    metric_colors = _metric_colors(toxic_cols)

    for idx in range(len(axes)):
        ax = axes[idx]
        if idx >= n_compounds:
            ax.axis("off")
            continue

        row_data = toxpi_agg.iloc[idx]
        name = row_data["compound"]
        score = row_data["toxpi"]

        scores_vector = np.nan_to_num(row_data[norm_cols].values.astype(float), nan=0.0)

        if beautify:
            min_thickness = 0.05
            scores_vector = np.where(scores_vector == 0.0, min_thickness, scores_vector)

        ax.set_theta_zero_location("N")
        ax.set_theta_direction(-1)

        ax.bar(
            starts,
            scores_vector,
            width=widths,
            bottom=0.2,
            color=[metric_colors[m] for m in toxic_cols],
            edgecolor="white",
            linewidth=0.4,
            align="edge",
        )
        ax.set_ylim(0, 1.2)
        ax.axis("off")

        ax.text(0.5, -0.15, name, fontsize=14, fontweight="bold", ha="center", va="top", transform=ax.transAxes)
        ax.text(0.5, -0.35, f"ToxPi: {score:.2f}", fontsize=11, color="black", ha="center", va="top",
                transform=ax.transAxes)

    layout_str = "Beautified Layout" if beautify else "Strict Original Layout"
    fig.suptitle(
        f"ToxPi Scores for All Pollutants ({n_compounds} compounds, {len(toxic_cols)} indicators) - {layout_str}",
        fontsize=16,
        y=0.92,
        ha="center",
    )

    legend_elements = [Patch(facecolor=metric_colors[col], label=col) for col in toxic_cols]
    legend_cols = min(5, max(1, len(toxic_cols)))
    fig.legend(
        handles=legend_elements,
        title="Toxicity Factor",
        title_fontsize=12,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.08),
        ncol=legend_cols,
        fontsize=10,
        frameon=False,
        labelspacing=0.5,
        handletextpad=0.5,
        columnspacing=1.2,
    )

    caption_text = "Radial length = Normalized value | Weights normalized to 100%"
    if beautify:
        caption_text += " | Zero values use minimum visual thickness"
    fig.text(0.94, 0.04, caption_text, fontsize=9.5, color="#333333", ha="right", va="bottom")
    return fig


def generate_toxpi_bar_plot(toxpi_agg, bar_colors_dict=None):
    compounds = toxpi_agg["compound"].values
    scores = toxpi_agg["toxpi"].values

    colors_list = []
    for comp in compounds:
        if bar_colors_dict and comp in bar_colors_dict:
            colors_list.append(bar_colors_dict[comp])
        else:
            colors_list.append("#4682B4")

    fig, ax = plt.subplots(figsize=(6.5, 4.6))

    bars = ax.bar(
        compounds,
        scores,
        color=colors_list,
        edgecolor="black",
        linewidth=0.8,
        width=0.42,
        alpha=0.9,
        zorder=3,
    )

    ax.set_title("各污染物 ToxPi 综合毒性得分对比图", fontsize=12, pad=15, fontweight="bold", color="#000000")
    ax.set_ylabel("ToxPi Score", fontsize=11, labelpad=8, color="#000000")

    ax.set_ymargin(0.15)
    ax.set_xlabel("")

    for bar in bars:
        height = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2.,
            height + 0.015,
            f"{height:.2f}",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
            color="#111111",
        )

    ax.grid(False)
    for spine in ["top", "right", "left", "bottom"]:
        ax.spines[spine].set_visible(True)
        ax.spines[spine].set_color("black")
        ax.spines[spine].set_linewidth(1.0)

    ax.tick_params(axis="both", which="both", direction="in", length=4, labelsize=10, colors="#000000")

    return fig


def export_results_to_excel(toxpi_agg, sensitivity_df, stats_dict, excel_path, top_k_value, toxic_cols=None):
    toxic_cols = get_toxic_cols_from_frame(toxpi_agg, toxic_cols)
    metadata_df = pd.DataFrame({
        "统计指标说明": [
            "蒙特卡洛模拟总次数 (Iterations)",
            f"排序频次统计阈值 (筛选前 Top {top_k_value} 名)",
            "Spearman相关系数均值 (Mean Rho)",
            "排名扰动标准差 (Standard Deviation)",
            "95% 置信区间下限 (2.5th Percentile)",
            "95% 置信区间上限 (97.5th Percentile)",
            "实际参与 ToxPi 计算的毒性指标数量",
        ],
        "对应数值": [
            1000,
            top_k_value,
            stats_dict["mean"],
            stats_dict["sd"],
            stats_dict["ci_lower"],
            stats_dict["ci_upper"],
            len(toxic_cols),
        ],
    })
    indicator_df = pd.DataFrame({"toxicity_indicator": toxic_cols})

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        toxpi_agg.to_excel(writer, sheet_name="ToxPi_Results", index=False)
        sensitivity_df.to_excel(writer, sheet_name="Sensitivity_Summary", index=False)
        metadata_df.to_excel(writer, sheet_name="Simulation_Metadata", index=False)
        indicator_df.to_excel(writer, sheet_name="Toxicity_Indicators", index=False)


def _metric_colors(toxic_cols):
    colors = {}
    cmap = plt.get_cmap("tab20")
    for idx, col in enumerate(toxic_cols):
        if col in METRIC_COLORS:
            colors[col] = METRIC_COLORS[col]
        elif idx < len(COLOR_PALETTE):
            colors[col] = COLOR_PALETTE[idx]
        else:
            rgba = cmap(idx % 20)
            colors[col] = "#{:02x}{:02x}{:02x}".format(
                int(rgba[0] * 255),
                int(rgba[1] * 255),
                int(rgba[2] * 255),
            )
    return colors


def _to_numeric_series(series):
    return pd.to_numeric(series.replace({"NA": np.nan, "na": np.nan, "": np.nan}), errors="coerce")


def _normalize_key(value):
    return str(value).strip().lower().replace(" ", "_").replace("-", "_")
