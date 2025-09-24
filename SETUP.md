# Web Scraping Setup Guide

This guide explains how to set up your environment for web scraping with Python. We will use a virtual environment to keep project dependencies isolated and install the core libraries needed for scraping and data handling. Instructions are provided for both Windows and Mac/Linux.

---

## 1. Install Python

Make sure you have Python 3.9 or later installed. You can download it from [python.org/downloads](https://www.python.org/downloads/).

Check your installation:

```bash
python --version   # or python3 --version on Mac/Linux
```

---

## 2. Create and Activate a Virtual Environment

A virtual environment allows you to manage dependencies for a project without affecting the global Python installation.

### Windows

```bash
# Create a virtual environment
python -m venv venv

# Activate the virtual environment
venv\Scripts\activate

# To deactivate when finished
deactivate
```

### Mac/Linux

```bash
# Create a virtual environment
python3 -m venv venv

# Activate the virtual environment
source venv/bin/activate

# To deactivate when finished
deactivate
```

---

## 3. Install Required Packages

Inside the activated virtual environment, install the libraries needed for web scraping:

```bash
pip install requests pandas beautifulsoup4 selenium webdriver-manager
```

- **requests**: For making HTTP requests and fetching web pages
- **pandas**: For data analysis and handling tabular data
- **beautifulsoup4**: For parsing and extracting information from HTML
- **selenium**: For browser automation and interacting with dynamic websites
- **webdriver-manager**: For automatically downloading and managing the correct browser drivers used with Selenium

---

## 4. Project Structure

A simple structure you can follow:

```
my-scraper/
│── venv/                # virtual environment
│── scraper.py           # your main script
│── requirements.txt     # list of dependencies
│── data/                # folder to store scraped data
```

You can create a `requirements.txt` file for sharing your dependencies:

```bash
pip freeze > requirements.txt
```
