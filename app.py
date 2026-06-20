"""Microgrid Optimizer — web front-end (Streamlit).

For your team: this is the only thing they open. They edit the Google Sheet, then click
"Run optimization" here and read the results. No Python or notebooks involved.

Run locally:   streamlit run app.py
Deploy free:   push this repo to GitHub, then https://share.streamlit.io -> New app -> app.py
"""
import matplotlib.pyplot as plt
import streamlit as st

import reporting
import run_microgrid
from excel_loader import DEFAULT_SOURCE

st.set_page_config(page_title="Microgrid Optimizer", page_icon="⚡", layout="wide")
st.title("⚡ Microgrid Optimizer")
st.caption("1) Edit the input Google Sheet.  2) Click **Run optimization**.  3) Read the results below.")

with st.sidebar:
    st.header("Inputs")
    source = st.text_input("Input Google Sheet URL", value=DEFAULT_SOURCE,
                           help="The sheet must be shared as 'Anyone with the link → Viewer'.")
    st.markdown(f"[↗ Open the input sheet]({source})")
    run = st.button("▶ Run optimization", type="primary", use_container_width=True)
    st.caption("A full-year run takes ~20 seconds.")


def _rating(sol, type_):
    return next((float(d["rating"]) for d in sol["data"] if d["type"] == type_), 0.0)


# Solve only when the button is pressed, then keep the result in session so that moving the
# week slider does not re-solve. Pressing Run again always re-reads the latest sheet.
if run:
    with st.spinner("Reading the sheet and solving…"):
        try:
            inputs, sol, sdf, status = run_microgrid.solve(source)
            st.session_state["result"] = (inputs, sol, sdf, str(status))
        except Exception as e:  # noqa: BLE001 - surface any input/solve error to the user
            st.session_state.pop("result", None)
            st.error(f"Could not run: {e}")

result = st.session_state.get("result")

if not result:
    st.info("⬅ Set the Sheet URL in the sidebar and click **Run optimization**.")
    st.stop()

inputs, sol, sdf, status = result
if "kOptimal" not in status:
    st.warning(f"Solver status: **{status}**. The model has no valid solution with these inputs — "
               "e.g. an off-grid setup (grid_max_fraction = 0) that can't meet the load. "
               "Adjust the Sheet and run again.")

# ---- headline metrics ----
es = reporting.energy_summary(sdf)
costs = sol["meta"]["costs"]
annual_cost = float(costs["LCoE"]) * float(costs["LOAD"])
c = st.columns(6)
c[0].metric("Solar", f"{_rating(sol, 'solar'):,.0f} kW")
c[1].metric("Battery", f"{_rating(sol, 'li'):,.0f} kWh")
c[2].metric("Diesel", f"{_rating(sol, 'diesel'):,.0f} kW")
c[3].metric("LCoE", f"${float(costs['LCoE']):.3f}/kWh")
c[4].metric("Monthly payment", f"${annual_cost / 12:,.0f}")
c[5].metric("Energy independence", f"{es['energy_independence_pct']:.0f}%")

tab_cost, tab_energy, tab_bom = st.tabs(["💰 Sizing & cost", "🔋 Energy mix", "📋 Bill of materials"])

with tab_cost:
    st.subheader("Technology size & cost")
    st.caption("Panels are priced without the inverter; the inverter is its own line.")
    st.dataframe(reporting.cost_table(inputs, sol), use_container_width=True, hide_index=True)
    st.subheader("LCoE & monthly payment")
    st.dataframe(reporting.lcoe_table(sol), use_container_width=True, hide_index=True)

with tab_energy:
    cc = reporting.co2_summary(sdf)
    m1, m2 = st.columns(2)
    m1.metric("Energy independence (share of load not from the grid)",
              f"{es['energy_independence_pct']:.1f}%")
    m2.metric("CO₂ saved / year vs 100% grid",
              f"{cc['offset_t']:,.0f} t", f"{cc['offset_pct']:.0f}% of {cc['baseline_t']:,.0f} t baseline")
    left, right = st.columns(2)
    with left:
        fig1, ax1 = plt.subplots(figsize=(5, 5))
        reporting.plot_energy_by_source(sdf, ax=ax1)
        st.pyplot(fig1)
    with right:
        fig3, ax3 = plt.subplots(figsize=(5, 5))
        reporting.plot_co2_pie(sdf, ax=ax3)
        st.pyplot(fig3)
    week = st.slider("Week of the year", 1, 52, 21)
    fig2, ax2 = plt.subplots(figsize=(10, 4))
    reporting.plot_dispatch(sdf, week=week, ax=ax2)
    st.pyplot(fig2)

with tab_bom:
    bom = reporting.bom_table(inputs, sol)
    st.dataframe(bom, use_container_width=True, hide_index=True)
    d1, d2 = st.columns(2)
    d1.download_button("⬇ Download BOM (CSV)", bom.to_csv(index=False), "bom.csv", "text/csv",
                       use_container_width=True)
    d2.download_button("⬇ Download hourly dispatch (CSV)", sdf.to_csv(index=False), "solution.csv",
                       "text/csv", use_container_width=True)
