import streamlit as st

st.set_page_config(page_title="GNS Automation Hub", layout="wide")

st.title("GNS Automation Hub")
st.caption("Four internal tools in one repo. Open one from this list or from the sidebar.")

st.write("")

st.subheader("Greentable & Gems Automator")
st.write(
    "Reads the weekly incentive screenshot with OCR, writes tier and vehicle rows into the "
    "Greentable sheet, then pushes the captured images into two Google Slides decks."
)
if st.button("Open Greentable", type="primary", key="open_greentable"):
    st.switch_page("pages/1_Greentable_and_Gems.py")

st.divider()

TOOLS = [
    (
        "Midweek to Endweek",
        "pages/2_Midweek_Endweek.py",
        "Censors promo screenshots with Gemini, pulls comments from the Sheet, then builds the "
        "Midweek and Endweek summary slides.",
    ),
    (
        "Monday Data Cleaner",
        "pages/3_Monday_Data_Cleaner.py",
        "Browser-driven cleanup of the SG/MY session spreadsheets, with results written back to "
        "the same workbook.",
    ),
    (
        "Weekly Presentation",
        "pages/4_Weekly_Presentation.py",
        "Fills the weekly report template placeholders (week range, Shopee and spending blocks) "
        "into a Google Slides deck.",
    ),
]

for name, page, desc in TOOLS:
    left, right = st.columns([5, 1], vertical_alignment="center")
    with left:
        st.markdown(f"**{name}**")
        st.write(desc)
    with right:
        if st.button("Open", key=f"open_{page}"):
            st.switch_page(page)
    st.divider()
