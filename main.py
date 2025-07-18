#!/usr/bin/env python3
"""
Autonomous Monitoring Agent using PocketFlow - FIXED VERSION
Continuously monitors AI/tech news, saves insights to CSV, and generates periodic reports.
Designed to run indefinitely without user interaction while managing API costs.
"""

import os
import json
import requests
import time
import csv
import datetime
from openai import OpenAI
from pathlib import Path

# PocketFlow implementation (100 lines)
import warnings, copy

class BaseNode:
    def __init__(self): self.params,self.successors={},{}
    def set_params(self,params): self.params=params
    def next(self,node,action="default"):
        if action in self.successors: warnings.warn(f"Overwriting successor for action '{action}'")
        self.successors[action]=node; return node
    def prep(self,shared): pass
    def exec(self,prep_res): pass
    def post(self,shared,prep_res,exec_res): pass
    def _exec(self,prep_res): return self.exec(prep_res)
    def _run(self,shared): p=self.prep(shared); e=self._exec(p); return self.post(shared,p,e)
    def run(self,shared): 
        if self.successors: warnings.warn("Node won't run successors. Use Flow.")  
        return self._run(shared)
    def __rshift__(self,other): return self.next(other)
    def __sub__(self,action):
        if isinstance(action,str): return _ConditionalTransition(self,action)
        raise TypeError("Action must be a string")

class _ConditionalTransition:
    def __init__(self,src,action): self.src,self.action=src,action
    def __rshift__(self,tgt): return self.src.next(tgt,self.action)

class Node(BaseNode):
    def __init__(self,max_retries=1,wait=0): super().__init__(); self.max_retries,self.wait=max_retries,wait
    def exec_fallback(self,prep_res,exc): raise exc
    def _exec(self,prep_res):
        for self.cur_retry in range(self.max_retries):
            try: return self.exec(prep_res)
            except Exception as e:
                if self.cur_retry==self.max_retries-1: return self.exec_fallback(prep_res,e)
                if self.wait>0: time.sleep(self.wait)

class Flow(BaseNode):
    def __init__(self,start=None): super().__init__(); self.start_node=start
    def start(self,start): self.start_node=start; return start
    def get_next_node(self,curr,action):
        nxt=curr.successors.get(action or "default")
        if not nxt and curr.successors: warnings.warn(f"Flow ends: '{action}' not found in {list(curr.successors)}")
        return nxt
    def _orch(self,shared,params=None):
        curr,p,last_action =copy.copy(self.start_node),(params or {**self.params}),None
        while curr: curr.set_params(p); last_action=curr._run(shared); curr=copy.copy(self.get_next_node(curr,last_action))
        return last_action
    def _run(self,shared): p=self.prep(shared); o=self._orch(shared); return self.post(shared,p,o)
    def post(self,shared,prep_res,exec_res): return exec_res

# Utility functions
def call_llm(prompt, max_tokens=300):
    """Call OpenAI API with cost-conscious settings"""
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Using mini for cost efficiency
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"LLM Error: {e}")
        return f"Error: {str(e)}"

def get_hackernews_stories():
    """Fetch recent Hacker News stories about AI/tech"""
    try:
        print("🔍 Fetching Hacker News top stories...")
        # Get top stories
        top_stories_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
        response = requests.get(top_stories_url, timeout=10)
        response.raise_for_status()
        story_ids = response.json()[:15]
        
        print(f"📋 Retrieved {len(story_ids)} story IDs, filtering for AI/tech content...")
        
        stories = []
        for i, story_id in enumerate(story_ids):
            try:
                story_url = f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
                story_response = requests.get(story_url, timeout=5)
                story_response.raise_for_status()
                story = story_response.json()
                
                if story and story.get('title'):
                    title = story['title'].lower()
                    if any(keyword in title for keyword in ['ai', 'llm', 'gpt', 'machine learning', 'artificial intelligence', 'tech', 'startup', 'programming', 'software', 'data', 'algorithm']):
                        stories.append({
                            'title': story['title'],
                            'url': story.get('url', ''),
                            'score': story.get('score', 0),
                            'time': story.get('time', 0),
                            'id': story_id
                        })
                        print(f"✅ Found relevant story: {story['title'][:60]}...")
                        
                        if len(stories) >= 5:
                            break
                    
            except Exception as e:
                print(f"⚠️  Error fetching story {story_id}: {e}")
                continue
                
        print(f"📰 Final collection: {len(stories)} relevant stories")
        return stories
        
    except Exception as e:
        print(f"❌ Error fetching Hacker News data: {e}")
        return []

def save_to_csv(data, filename):
    """Save data to CSV file with proper header handling"""
    try:
        file_exists = Path(filename).exists()
        
        # Ensure data is in the right format
        if isinstance(data, dict):
            data_list = [data]
        elif isinstance(data, list):
            data_list = data
        else:
            print(f"⚠️  Invalid data type for CSV: {type(data)}")
            return
        
        if not data_list:
            print(f"⚠️  No data to save to {filename}")
            return
            
        fieldnames = data_list[0].keys()
        
        with open(filename, 'a', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            # Only write header if file is new or empty
            if not file_exists or os.path.getsize(filename) == 0:
                writer.writeheader()
            
            writer.writerows(data_list)
        
        print(f"💾 Saved {len(data_list)} record(s) to {filename}")
        
    except Exception as e:
        print(f"❌ Error saving to CSV {filename}: {e}")

def load_seen_stories():
    """Load previously seen story titles from CSV to avoid duplicates"""
    seen_stories = set()
    seen_stories_file = 'seen_stories.csv'
    
    if Path(seen_stories_file).exists() and os.path.getsize(seen_stories_file) > 0:
        try:
            with open(seen_stories_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if 'title' in row and row['title'].strip():
                        seen_stories.add(row['title'].strip())
        except Exception as e:
            print(f"⚠️  Could not load seen stories: {e}")
    
    return seen_stories

def save_seen_story(title):
    """Save a story title to the seen stories file"""
    try:
        if not title or not title.strip():
            return
            
        story_data = {
            'title': title.strip(),
            'first_seen': datetime.datetime.now().isoformat()
        }
        save_to_csv(story_data, 'seen_stories.csv')
    except Exception as e:
        print(f"⚠️  Could not save seen story: {e}")

# Agent Nodes
class DataCollectionNode(Node):
    """Collect recent tech news data"""
    
    def prep(self, shared):
        print(f"[{datetime.datetime.now()}] Starting data collection...")
        try:
            seen_stories = load_seen_stories()
            shared['seen_stories'] = seen_stories
            print(f"📚 Loaded {len(seen_stories)} previously seen stories")
            return None
        except Exception as e:
            print(f"⚠️  Error loading seen stories: {e}")
            shared['seen_stories'] = set()
            return None
    
    def exec(self, prep_res):
        try:
            stories = get_hackernews_stories()
            return stories
        except Exception as e:
            print(f"❌ Data collection failed: {e}")
            return []
    
    def exec_fallback(self, prep_res, exc):
        print(f"❌ Data collection fallback triggered: {exc}")
        return []
    
    def post(self, shared, prep_res, exec_res):
        try:
            seen_stories = shared.get('seen_stories', set())
            new_stories = []
            
            print(f"🔍 Processing {len(exec_res)} stories...")
            
            # Filter out stories we've already seen
            for story in exec_res:
                title = story['title'].strip()
                if title not in seen_stories:
                    new_stories.append(story)
                    # Mark story as seen and save it
                    save_seen_story(title)
                    seen_stories.add(title)
                    print(f"✨ New story: {title[:50]}...")
                else:
                    print(f"🔄 Skipping seen story: {title[:50]}...")
            
            shared['raw_stories'] = new_stories
            shared['collection_time'] = datetime.datetime.now().isoformat()
            
            if new_stories:
                print(f"✅ Found {len(new_stories)} new stories (filtered {len(exec_res) - len(new_stories)} duplicates)")
                return "analyze"
            else:
                print(f"📰 No new stories found (all {len(exec_res)} were duplicates or no stories available)")
                return "wait"
                
        except Exception as e:
            print(f"❌ Error in data collection post-processing: {e}")
            return "wait"

class AnalysisNode(Node):
    """Analyze collected stories using LLM"""
    
    def prep(self, shared):
        stories = shared.get('raw_stories', [])
        if not stories:
            return None
        
        # Create a concise summary of stories for analysis
        story_summary = "\n".join([f"- {story['title']} (Score: {story['score']})" 
                                  for story in stories[:3]])
        print(f"🧠 Analyzing {len(stories)} stories...")
        return story_summary
    
    def exec(self, prep_res):
        if not prep_res:
            return "No stories to analyze"
        
        # Simplified prompt for better parsing
        prompt = f"""Analyze these tech/AI news headlines. Provide ONLY:
Theme (max 2 words)|Sentiment (positive/negative/neutral)|Brief insight (max 40 words)

Headlines:
{prep_res}

Return format: Theme|Sentiment|Insight"""
        
        return call_llm(prompt, max_tokens=150)
    
    def exec_fallback(self, prep_res, exc):
        print(f"❌ Analysis fallback triggered: {exc}")
        return "Error|Neutral|Analysis failed due to API error"
    
    def post(self, shared, prep_res, exec_res):
        try:
            print(f"🧠 LLM Response: {exec_res}")
            
            # Clean and parse LLM response - take only first line if multiline
            clean_response = exec_res.split('\n')[0].strip()
            parts = clean_response.split('|')
            
            if len(parts) >= 3:
                theme = parts[0].strip()
                sentiment = parts[1].strip().lower()
                insight = parts[2].strip()
                
                # Clean up theme (remove numbers, extra formatting)
                theme = theme.replace('1.', '').replace('2.', '').replace('3.', '').strip()
                
                # Ensure sentiment is valid
                if sentiment not in ['positive', 'negative', 'neutral']:
                    sentiment = 'neutral'
            else:
                theme = "Mixed Topics"
                sentiment = "neutral"
                insight = clean_response[:40] if clean_response else "No insight available"
                
            analysis = {
                'timestamp': shared.get('collection_time', datetime.datetime.now().isoformat()),
                'theme': theme,
                'sentiment': sentiment,
                'insight': insight,
                'story_count': len(shared.get('raw_stories', []))
            }
            
            shared['analysis'] = analysis
            print(f"✅ Analysis complete: {theme} ({sentiment}) - {insight}")
            return "default"
            
        except Exception as e:
            print(f"❌ Analysis post-processing error: {e}")
            # Create fallback analysis
            fallback_analysis = {
                'timestamp': shared.get('collection_time', datetime.datetime.now().isoformat()),
                'theme': 'Error',
                'sentiment': 'neutral',
                'insight': f'Analysis failed: {str(e)[:40]}',
                'story_count': len(shared.get('raw_stories', []))
            }
            shared['analysis'] = fallback_analysis
            return "default"

class SaveDataNode(Node):
    """Save analysis results to CSV"""
    
    def prep(self, shared):
        analysis = shared.get('analysis', {})
        print(f"💾 Preparing to save: {analysis}")
        return analysis
    
    def exec(self, prep_res):
        if prep_res and isinstance(prep_res, dict) and 'theme' in prep_res:
            try:
                save_to_csv(prep_res, 'agent_insights.csv')
                return "Data saved successfully"
            except Exception as e:
                print(f"❌ Error saving to CSV: {e}")
                return f"Save error: {e}"
        return "No valid data to save"
    
    def exec_fallback(self, prep_res, exc):
        print(f"❌ Save fallback triggered: {exc}")
        return "Save failed - continuing anyway"
    
    def post(self, shared, prep_res, exec_res):
        print(f"💾 {exec_res}")
        
        # Clear processed data but keep seen_stories for next cycle
        shared.pop('raw_stories', None)
        shared.pop('analysis', None)
        
        return "default"

class WaitNode(Node):
    """Wait before next collection cycle"""
    
    def prep(self, shared):
        # Wait time: 15 minutes to be conservative with API usage
        wait_minutes = 15
        return wait_minutes
    
    def exec(self, prep_res):
        wait_seconds = prep_res * 60
        print(f"Waiting {prep_res} minutes before next cycle...")
        time.sleep(wait_seconds)
        return f"Waited {prep_res} minutes"
    
    def post(self, shared, prep_res, exec_res):
        print(f"[{datetime.datetime.now()}] Wait complete. Ready for report check.")
        return "default"

class ReportNode(Node):
    """Generate periodic summary report"""
    
    def prep(self, shared):
        # Check if it's time for a daily report
        now = datetime.datetime.now()
        last_report_file = 'last_report.txt'
        
        try:
            if Path(last_report_file).exists():
                with open(last_report_file, 'r') as f:
                    last_report_date = datetime.datetime.fromisoformat(f.read().strip())
                
                # Generate report if more than 24 hours since last report
                if (now - last_report_date).total_seconds() > 24 * 3600:
                    return True
            else:
                return True  # First run
        except:
            return True
        
        return False
    
    def exec(self, prep_res):
        if not prep_res:
            return "No report needed"
        
        # Read recent insights from CSV
        try:
            insights = []
            if Path('agent_insights.csv').exists() and os.path.getsize('agent_insights.csv') > 0:
                with open('agent_insights.csv', 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    insights = list(reader)[-10:]  # Last 10 entries
            
            if insights:
                # Generate summary report
                themes = [i.get('theme', 'Unknown') for i in insights if 'theme' in i]
                sentiments = [i.get('sentiment', 'neutral') for i in insights if 'sentiment' in i]
                
                if themes and sentiments:
                    prompt = f"""Create a brief daily summary based on these tech trends:
Themes: {', '.join(themes)}
Sentiments: {', '.join(sentiments)}

Provide: 1 key trend, overall sentiment, 1 prediction (max 80 words total)"""
                    
                    summary = call_llm(prompt, max_tokens=150)
                    
                    # Save report
                    report_data = {
                        'date': datetime.datetime.now().date().isoformat(),
                        'insights_analyzed': len(insights),
                        'summary': summary[:200]  # Limit summary length
                    }
                    
                    save_to_csv(report_data, 'daily_reports.csv')
                    
                    # Update last report time
                    with open('last_report.txt', 'w') as f:
                        f.write(datetime.datetime.now().isoformat())
                    
                    return f"Daily report generated: {summary[:100]}..."
                else:
                    return "No valid themes found for report"
            
            return "No data for report"
            
        except Exception as e:
            return f"Report error: {e}"
    
    def post(self, shared, prep_res, exec_res):
        if "generated" in exec_res:
            print(f"📊 {exec_res}")
        else:
            print(f"📊 Report check: {exec_res}")
        
        print(f"🔄 Cycle complete - flow will restart")
        return None

def create_autonomous_agent():
    """Create the autonomous monitoring agent flow"""
    
    # Create nodes
    collect_node = DataCollectionNode(max_retries=2, wait=5)
    analyze_node = AnalysisNode(max_retries=2, wait=3)
    save_node = SaveDataNode()
    wait_node = WaitNode()
    report_node = ReportNode()
    
    # Create a simple linear flow that loops back to start
    collect_node - "analyze" >> analyze_node
    collect_node - "wait" >> wait_node  # Skip analysis if no stories
    
    analyze_node >> save_node  # Always save after analysis
    save_node >> wait_node     # Always wait after saving
    wait_node >> report_node   # Check for daily report
    
    # report_node ends the flow, causing main loop to restart
    
    return Flow(start=collect_node)

def main():
    """Main execution function"""
    print("🤖 Starting Autonomous Monitoring Agent (FIXED VERSION)")
    print("📊 Monitoring tech/AI news and generating insights")
    print("💾 Data saved to: agent_insights.csv, daily_reports.csv, seen_stories.csv")
    print("🔄 Duplicate detection: FIXED (saves tokens)")
    print("⏰ Collection cycle: 15 minutes")
    print("🛑 Press Ctrl+C to stop\n")
    
    # Verify API key
    if not os.getenv("OPENAI_API_KEY"):
        print("❌ Error: OPENAI_API_KEY environment variable not set")
        return
    
    # Create shared data store
    shared = {
        'agent_start_time': datetime.datetime.now().isoformat(),
        'cycles_completed': 0
    }
    
    # Create the agent
    agent = create_autonomous_agent()
    
    try:
        while True:
            cycle_start = datetime.datetime.now()
            print(f"\n🔄 Cycle {shared['cycles_completed'] + 1} - {cycle_start}")
            
            try:
                # Run one complete cycle
                result = agent.run(shared)
                print(f"🔗 Flow result: {result}")
                
                shared['cycles_completed'] += 1
                cycle_duration = datetime.datetime.now() - cycle_start
                print(f"✅ Cycle completed in {cycle_duration.total_seconds():.1f} seconds")
                
            except Exception as e:
                print(f"⚠️  Cycle error: {e}")
                print(f"🔍 Error details: {type(e).__name__}: {str(e)}")
                print("🔄 Continuing to next cycle...")
                time.sleep(60)  # Wait 1 minute before retry
                continue
            
    except KeyboardInterrupt:
        print(f"\n🛑 Agent stopped by user after {shared['cycles_completed']} cycles")
        print(f"📈 Total runtime: {datetime.datetime.now() - datetime.datetime.fromisoformat(shared['agent_start_time'])}")
    except Exception as e:
        print(f"\n❌ Agent error: {e}")
        print("🔄 Agent will restart automatically if run again")

if __name__ == "__main__":
    main()