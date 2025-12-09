import os
import time
import requests
from typing import Optional

class ContentFactoryClient:
    def __init__(self):
        self.base_url = os.getenv("CONTENT_FACTORY_URL")  # e.g. http://1.2.3.4:8000
        self.api_key = os.getenv("CONTENT_FACTORY_API_KEY")
        self.headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json"
        }

    def generate_article(
        self, 
        domain: str, 
        competitors: list[str],
        topic_preference: Optional[str] = None,
        target_audience: Optional[str] = None,
        keywords: Optional[list[str]] = None,
        additional_context: Optional[str] = None,
        auto_publish: bool = True
    ) -> dict:
        """
        Runs the full pipeline and waits for the result.
        
        Args:
            domain: Target domain for the article
            competitors: List of competitor domains
            topic_preference: Optional topic/angle preference
            target_audience: Optional target audience description
            keywords: Optional list of keywords to target
            additional_context: Optional additional context
            auto_publish: Whether to automatically publish after generation
            
        Returns:
            Result dict with article info and optionally publish data
        """
        # 1. Start Job
        payload = {
            "domain": domain,
            "competitors": competitors
        }
        
        # Add optional context if provided
        if topic_preference:
            payload["topic_preference"] = topic_preference
        if target_audience:
            payload["target_audience"] = target_audience
        if keywords:
            payload["keywords"] = keywords
        if additional_context:
            payload["additional_context"] = additional_context
        
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
            
            result = result_res.json().get("result", {})
            result["job_id"] = job_id
            
            # 4. Auto-publish if enabled
            if auto_publish:
                try:
                    publish_result = self.publish_article(job_id)
                    result["publish"] = publish_result
                except Exception as e:
                    print(f"Auto-publish failed: {e}")
                    result["publish_error"] = str(e)
            
            return result
            
        except requests.exceptions.RequestException as e:
            print(f"Content Factory API Error: {e}")
            raise Exception(f"Failed to communicate with Content Factory: {e}")

    def publish_article(self, job_id: str) -> dict:
        """
        Publish a completed article via the publish endpoint.
        
        Args:
            job_id: The job ID of the completed article
            
        Returns:
            Dict with:
                - preview_url: Cloudflare preview URL (may be None)
                - pr_url: GitHub Pull Request URL
                - pr_number: PR number
                - branch_name: Git branch name
                - branch_url: URL to the branch
                - file_path: Path to the created file
                - message: Status message
        """
        try:
            response = requests.post(
                f"{self.base_url}/api/pipeline/publish/{job_id}",
                headers=self.headers
            )
            response.raise_for_status()
            
            data = response.json()
            
            if data.get("status") == "success":
                publish_data = data.get("data", {})
                return {
                    "success": True,
                    "preview_url": publish_data.get("preview_url"),
                    "pr_url": publish_data.get("pr_url"),
                    "pr_number": publish_data.get("pr_number"),
                    "branch_name": publish_data.get("branch_name"),
                    "branch_url": publish_data.get("branch_url"),
                    "file_path": publish_data.get("file_path"),
                    "message": publish_data.get("message", "Content published successfully")
                }
            else:
                error = data.get("error", "Unknown publish error")
                raise Exception(f"Publish failed: {error}")
                
        except requests.exceptions.RequestException as e:
            print(f"Publish API Error: {e}")
            raise Exception(f"Failed to publish article: {e}")
