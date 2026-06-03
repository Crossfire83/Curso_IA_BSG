# system-interconnectivity-rag

## Getting started

- Verify your python installation with `python3 --version` for mac or for windows: `python --version`
  - If not installed, you have to install [Python](https://www.python.org/downloads/)
- Verify pip is installed with `python3 -m pip --version` for mac or for windows: `python -m pip --version`
- Install "Python" and "Python Debugger" extensions in your IDE.

Make sure that you have the credentials file of AWS (~/.aws/credentials) and that you have access to bedrock and some models hosted there.

Also, make sure you set up these environment variables:

```shell
export HF_HUB_ETAG_TIMEOUT=86400
export HF_HUB_DOWNLOAD_TIMEOUT=86400
export HF_TOKEN=<your_huggingface_token>
```

Then, run the following script in your terminal

For macOs:

```shell
python3 -m venv .venv # create a virtual environment for your project
source .venv/bin/activate # activate the virtual environment in your project
```

For windows:

```shell
python -m venv .venv # create a virtual environment for your project
.venv\Scipts\activate # activate the virtual environment in your project
```

Then install the python dependencies with:
`pip install -r requirements.txt`

## Steps for deployment

1. Create your account in [AWS](https://aws.amazon.com/free/)
   1. Create your access key. This will help us with the interconnection of cloud providers. (Azure to AWS)
      1. Go to the menu on the top-right corner of your aws console screen.
      2. Click on security credentials
      3. Go to the Access Keys section
      4. Click on `Create Access Key`
      5. Copy all the values on a notepad or text editor or environment variables file. (follow the `.env.sample` file)
   2. Create an S3 bucket. This will help us store our knowledge base
      1. Setup a namespace for the bucket.
      2. Disable ACLs.
      3. Block public access.
      4. Disable bucket versioning.
      5. Configure default encryption.
      6. Disable Object lock.
   3. For the Bedrock Model, this project already uses Bedrock integration, so no additional setup is needed.

2. Create your [Azure](https://portal.azure.com) account.
   1. Create a Subscription (name it however you want)
   2. Create a Resource Group inside the Subscription created in step 2.1 (name it however you want)
      1. Select the region most convenient to you (e.g. south central US)
   3. Create an App Service
      1. Select just "Web App" when you click on the create button.
         1. Select the Subscription and Resource Group created on steps 2.1 and 2.2 respectively
         2. Name your webapp however you want.
         3. Select the option "Container" on the Publish field (radio button)
         4. Select operating system, by default and conveniently is linux.
         5. Select the region most convenient to you (e.g. south central US)
         6. Select the linux plan, or create a new one if you dont have one.
         7. Select the pricing plan. (For this project I'll select "Free F1")
      2. Click "Next: Database >"
         1. DO NOT create a database.
      3. Click "Next: Container >"
         1. Disable Sidecar support.
         2. Select image source: other container registries.
         3. Access type: Public
         4. Registry server URL: https://index.docker.io
         5. Image and tag: <name_of_your_docker_container>:<version>
         6. Startup command: gunicorn app:app
      4. Click "Next: Networking >"
         1. Enable public access.
      5. Click "Next: Monitor + secure >"
      6. Click "Next: Tags >". (Optionally add the tags you want).
      7. Click "Next: Review + create >"
      8. Review the settings you entered and finally click "Create".
