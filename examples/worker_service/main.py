import time
import sys
import logging

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("DummyWorker")

def main():
    logger.info("Worker started.")
    job_id = 1
    
    while True:
        logger.info(f"Processing job #{job_id}...")
        
        # Simulate work
        time.sleep(5)
        
        logger.info(f"Job #{job_id} completed successfully.")
        job_id += 1
        
        # Optionally simulate a failure randomly
        if job_id % 10 == 0:
            logger.warning(f"Job #{job_id} encountered a transient error! Retrying...")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Worker shutting down gracefully.")
        sys.exit(0)
