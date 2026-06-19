### **Threat Intelligence API Prototype**



#### **Description**



This proof-of-concept demonstrates how external threat intelligence sources can be integrated into an Email Threat Intelligence Aggregator.



The prototype enriches email indicators using:



\* AbuseIPDB (Sender IP reputation)

\* Google Safe Browsing (URL reputation)



The APIs provide up-to-date threat intelligence data, while the analysis and risk scoring logic are implemented in Python.



#### **Features**



\* Accepts a sender IP address and URL as input

\* Queries AbuseIPDB for sender reputation

\* Queries Google Safe Browsing for URL reputation

\* Generates a final risk score

\* Produces a threat intelligence report



#### **Installation**



Install required packages:



```bash

pip install -r requirements.txt

```



### **Setup**



1\. Copy `.env.example`

2\. Rename it to `.env`

3\. Add your API keys:



```text

ABUSEIPDB\_API\_KEY=your\_key\_here

GOOGLE\_SAFE\_BROWSING\_API\_KEY=your\_key\_here

```



### **Running the Program**



```bash

python capstone\_api\_test.py

```



### **Example Output**



The program outputs:



\* AbuseIPDB analysis

\* Google Safe Browsing analysis

\* Final risk score

\* Risk verdict (Low, Medium, High, Critical)



