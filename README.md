# implementation-sample-api-python

This is a SaaS implementation sample using the SaaSus SDK

See the documentation [API implementation using SaaS Platform](https://docs.saasus.io/ja/docs/implementation-guide/implementing-authentication-using-saasus-platform-apiserver)

## Run Python API

```
git clone git@github.com:saasus-platform/implementation-sample-api-python.git
cd ./implementation-sample-api-python
```

```
cp .env.example .env
vi .env

# Set Env for SaaSus Platform API
# Get it in the SaaSus Admin Console
export SAASUS_SAAS_ID="xxxxxxxxxx"
export SAASUS_API_KEY="xxxxxxxxxx"
export SAASUS_SECRET_KEY="xxxxxxxxxx"

# Save and exit
```

```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install git+https://github.com/saasus-platform/saasus-sdk-python.git
sudo uvicorn main:app --port 80 --reload
```
