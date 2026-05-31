# system-interconnectivity-rag

## Getting started

- Verify your python installation with `python3 --version`
  - If not installed, you have to install [Python](https://www.python.org/downloads/)
- Verify pip is installed with `python3 -m pip --version`
- Install "Python" and "Python Debugger" extensions in your IDE.

Make sure that you have the credentials file of AWS (~/.aws/credentials) and that you have access to bedrock and some models hosted there.

Make sure you create/modify the file for pip to grab libraries from artifactory

Make sure that you get your credentials from jfrog:

1. set me up
2. click on PyPi
3. set the client as pip
4. replace the index-url below with the value provided by artifactory.

$HOME/.pip/pip.conf:

```
[global]
index-url = https://JFROG_USER:JFROG_PASSWORD@dishwireless.jfrog.io/artifactory/api/pypi/dev-pypi/simple
trusted-host = dishwireless.jfrog.io
               pypi.org
               files.pythonhosted.org
               conda.anaconda.org
```

Also, make sure you set up these environment variables:
These credentials can be obtained via artifactory -> set me up -> huggingface

```shell
export HF_HUB_ETAG_TIMEOUT=86400
export HF_HUB_DOWNLOAD_TIMEOUT=86400
export HF_ENDPOINT=https://dishwireless.jfrog.io/artifactory/api/huggingfaceml/hugging-face-remote
export HF_TOKEN=<your_huggingface_token>
```

Then, run the following script in your terminal

```shell
python3 -m venv .venv # create a virtual environment for your project
source .venv/bin/activate # activate the virtual environment in your project
```

Then install the python dependencies with:
`pip install -r requirements.txt`
