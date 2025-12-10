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
        topic: str,
        target_keyword: str,
        context: Optional[str] = None
    ) -> str:
        """
        Direct Mode: Generates an article for a specific topic/keyword.
        
        Args:
            domain: User's domain
            topic: Specific topic title (e.g. "AI Hackathons")
            target_keyword: Main keyword to target (e.g. "hackathon melbourne")
            context: Optional conversation context/thread history
            
        Returns:
            job_id: The ID of the generation job
        """
        payload = {
            "domain": domain,
            "topic": topic,
            "target_keyword": target_keyword,
        }
        if context:
            payload["context"] = context
            
        try:
            response = requests.post(
                f"{self.base_url}/api/pipeline/generate", 
                json=payload, 
                headers=self.headers
            )
            response.raise_for_status()
            
            data = response.json()
            job_id = data.get("job_id")
            
            if not job_id:
                raise Exception("No job_id returned from generate endpoint")
                
            print(f"Generate Job started: {job_id}")
            return job_id
            
        except requests.exceptions.RequestException as e:
            print(f"Content Factory Generate API Error: {e}")
            raise Exception(f"Failed to start generation: {e}")

    def discover_opportunities(
        self,
        domain: str,
        competitors: list[str],
        seed_keywords: Optional[list[str]] = None
    ) -> list[dict]:
        """
        Discovery Mode: Analyzes competitors to find content opportunities.
        
        Args:
            domain: User's domain
            competitors: List of competitor domains to analyze
            seed_keywords: Optional hints for discovery
            
        Returns:
            List of opportunity dicts (keyword, volume, difficulty, etc)
        """
        payload = {
            "domain": domain,
            "competitors": competitors
        }
        if seed_keywords:
            payload["seed_keywords"] = seed_keywords
            
        try:
            response = requests.post(
                f"{self.base_url}/api/pipeline/discover",
                json=payload,
                headers=self.headers
            )
            response.raise_for_status()
            
            data = response.json()
            if data.get("status") != "success":
                raise Exception(f"Discovery failed: {data.get('error')}")
                
            return data.get("opportunities", [])
            
        except requests.exceptions.RequestException as e:
            print(f"Content Factory Discover API Error: {e}")
            raise Exception(f"Failed to discover opportunities: {e}")

    def get_job_status(self, job_id: str) -> dict:
        """Get the current status of a job."""
        try:
            status_res = requests.get(
                f"{self.base_url}/api/pipeline/status/{job_id}",
                headers=self.headers
            )
            status_res.raise_for_status()
            return status_res.json()
        except requests.exceptions.RequestException as e:
            print(f"Status check failed: {e}")
            raise

    def get_job_result(self, job_id: str) -> dict:
        """Get the final result of a completed job."""
        try:
            result_res = requests.get(
                f"{self.base_url}/api/pipeline/result/{job_id}",
                headers=self.headers
            )
            result_res.raise_for_status()
            return result_res.json().get("result", {})
        except requests.exceptions.RequestException as e:
            print(f"Result fetch failed: {e}")
            raise

    def poll_and_wait(self, job_id: str) -> dict:
        """Helper to poll a job until completion and return result."""
        while True:
            status_data = self.get_job_status(job_id)
            state = status_data["status"]
            progress = status_data.get("progress", 0)
            step = status_data.get("current_step", "unknown")
            
            print(f"Status: {state} ({progress}%) - {step}")
            
            if state == "completed":
                break
            elif state == "failed":
                error_msg = status_data.get("error", "Unknown error")
                raise Exception(f"Job failed: {error_msg}")
            
            time.sleep(5)
            
        return self.get_job_result(job_id)

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
