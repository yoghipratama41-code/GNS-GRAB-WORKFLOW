# Greentable and Gems Extraction Automation

## 📌 Overview
Performing manual checks across all three aspects of Greentable and Gems extraction is a highly time-consuming process. Transitioning from the manual checking phase to applying the updates and generating the final reports creates additional bottlenecks. 

This automation project was designed to eliminate these inefficiencies. By streamlining the workflow, it automatically updates Greentable and Gems data and generates the required presentation slides, significantly saving time and boosting overall efficiency.

## ✨ Key Features
- **Automated Image Extraction**: Automatically processes and extracts data from uploaded pictures that follow a specific format. The extracted results are then routed directly into the designated information directory.
- **Automated Slide Reporting**: Seamlessly takes the updated extraction data and automatically generates the necessary reports directly onto presentation slides, cutting out the manual formatting process.

## ⚠️ Important Notes & Limitations
- **Human-in-the-Loop**: While the extraction is automated, this process still requires a manual double-check of the final results to ensure complete accuracy before finalizing the reports.
- **Usage Quotas**: This automation relies on free tools. As a result, it is subject to standard daily usage quotas and rate limits associated with the underlying services.

## 🚀 Getting Started (Usage Steps)
1. **Compile and Organize Images**: Gather the new pictures containing the Greentable and Gems data. 
   * *Crucial Note*: for the gems, the date input will be read from the image file name. 
   * *Recommendation*: when renaming the file name, use today's date and month to not confuse the automation.
2. **Update the Directory**: Simply place or update these formatted pictures in your designated working directory. The automation will take over to process the data, update the directories, and generate the slide reports.

## 🛠️ Tech Stack
- **Python**: The core language used to build the data extraction pipeline, handle directory routing, and automate the slide reporting process.
- **Gemini LLM**: Leveraged to process, synthesize, and summarize the qualitative information efficiently without relying on paid APIs.
