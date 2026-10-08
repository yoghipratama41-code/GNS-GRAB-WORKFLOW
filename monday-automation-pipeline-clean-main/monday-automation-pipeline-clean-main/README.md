# Data Cleaner Automation

## 📌 Overview
This Streamlit application automates the process of fetching data from a source Google Sheet (e.g., SG2, MY2). It then cleans the data by processing it through a web-based cleaning tool using Selenium. Finally, it uploads the cleaned results directly to a target Google Sheet (e.g., SG3, MY3).

## ✨ Key Features
* Retrieves data from a source Google Sheet.
* Automates web interactions using Selenium to upload and clean the data via the web pipeline.
* Automatically selects the "Grab only" configuration during the web cleaning process.
* Uploads the processed, clean data back into a designated target Google Sheet.
* Supports headless browser execution, which is highly recommended for server deployments.
* Handles Google Service Account authentication via a manual `credentials.json` file upload directly in the app interface.

## 🛠️ Dependencies
Ensure you have the following packages installed (typically listed in a `requirements.txt` file):
* `streamlit`
* `selenium`
* `gspread`
* `pandas`
* `webdriver-manager`

## 🚀 Getting Started

**1. Install Dependencies**
Install the required Python packages mentioned above.

**2. Setup Credentials**
The application requires Google Service Account credentials to access Google Sheets. You can easily upload your `credentials.json` file directly within the application's UI when it runs.

**3. Run the Application**
Execute the following command in your terminal to start the Streamlit app:
> streamlit run streamlit_app.py

## 🔄 Usage Flow
Once the app is running, you can:
1. Verify your connection settings and ensure the Google Spreadsheet URL and Pipeline URL are correct.
2. Select the specific sessions you wish to run (e.g., mapping source sheet "SG2" to target sheet "SG3").
3. Click "Run Automation" to execute the pipeline.
4. Monitor the live logs and download the resulting cleaned CSV files directly from the interface once the process completes.
