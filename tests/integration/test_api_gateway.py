import os

import boto3
import pytest
import requests

"""
Make sure env variable AWS_SAM_STACK_NAME exists with the name of the stack we are going to test. 
"""


class TestApiGateway:

    @pytest.fixture()
    def api_gateway_url(self):
        """ Get the API Gateway URL from Cloudformation Stack outputs """
        stack_name = os.environ.get("AWS_SAM_STACK_NAME")

        if stack_name is None:
            raise ValueError('Please set the AWS_SAM_STACK_NAME environment variable to the name of your stack')

        client = boto3.client("cloudformation")

        try:
            response = client.describe_stacks(StackName=stack_name)
        except Exception as e:
            raise Exception(
                f"Cannot find stack {stack_name} \n" f'Please make sure a stack with the name "{stack_name}" exists'
            ) from e

        stacks = response["Stacks"]
        stack_outputs = stacks[0]["Outputs"]
        api_outputs = [output for output in stack_outputs if output["OutputKey"] == "NotificationApi"]

        if not api_outputs:
            raise KeyError(f"NotificationApi not found in stack {stack_name}")

        return api_outputs[0]["OutputValue"]  # Extract url from stack outputs

    def test_missing_required_field_returns_400(self, api_gateway_url):
        """ Calling the endpoint without required fields should fail validation, not send an email """
        response = requests.post(api_gateway_url, json={"appName": "cavetools"})

        assert response.status_code == 400
        body = response.json()
        assert body["success"] is False

    def test_unsupported_app_name_returns_400(self, api_gateway_url):
        response = requests.post(api_gateway_url, json={
            "appName": "not-a-real-app",
            "notificationType": "new_lead",
            "recipientType": "owner",
            "data": {
                "name": "Test",
                "email": "test@example.com",
                "phone": "0000000000",
                "businessType": "Test",
                "message": "Test",
            },
        })

        assert response.status_code == 400
        assert response.json() == {"success": False, "message": "Unsupported appName"}
