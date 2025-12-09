import os
import time
import requests

class ContentFactoryClient:
    def __init__(self):
        self.base_url = os.getenv("CONTENT_FACTORY_URL")  # e.g. http://1.2.3.4:8000
        self.api_key = os.getenv("CONTENT_FACTORY_API_KEY")
        self.headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json"
        }

    def generate_article(self, domain: str, competitors: list[str]) -> dict:
        """
        Runs the full pipeline and waits for the result.
        Returns the final result dict or raises Exception.
        """
        # 1. Start Job
        payload = {
            "domain": domain,
            "competitors": competitors
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/api/pipeline/start", 
                json=payload, 
                headers=self.headers
            )
            response.raise_for_status()
            job_id = response.json().get("job_id")
            if not job_id:
                raise Exception("No job_id returned from start pipeline")
                
            print(f"Job started: {job_id}")
            
            # 2. Poll for Completion
            while True:
                status_res = requests.get(
                    f"{self.base_url}/api/pipeline/status/{job_id}",
                    headers=self.headers
                )
                status_res.raise_for_status()
                status_data = status_res.json()
                
                state = status_data["status"]
                progress = status_data.get("progress", 0)
                step = status_data.get("current_step", "unknown")
                
                print(f"Status: {state} ({progress}%) - {step}")
                
                if state == "completed":
                    break
                elif state == "failed":
                    error_msg = status_data.get("error", "Unknown error")
                    raise Exception(f"Job failed: {error_msg}")
                
                time.sleep(5)  # Poll every 5 seconds
                
            # 3. Get Result
            result_res = requests.get(
                f"{self.base_url}/api/pipeline/result/{job_id}",
                headers=self.headers
            )
            result_res.raise_for_status()
            
            return result_res.json().get("result", {})
            
        except requests.exceptions.RequestException as e:
            print(f"Content Factory API Error: {e}")
            raise Exception(f"Failed to communicate with Content Factory: {e}")
