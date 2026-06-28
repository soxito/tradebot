"""
Sentiment Analysis Engine
Uses VADER and TextBlob for crypto news sentiment analysis
"""
from typing import Dict, List, Optional
import nltk
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from textblob import TextBlob
from loguru import logger


class SentimentAnalyzer:
    """
    Multi-algorithm sentiment analyzer for crypto news and social media
    """
    
    def __init__(self):
        """Initialize sentiment analyzers"""
        try:
            # Initialize VADER
            self.vader = SentimentIntensityAnalyzer()
            
            # Enhance VADER lexicon with crypto-specific terms
            crypto_lexicon_updates = {
                "moon": 3.5,
                "mooning": 3.5,
                "bullish": 3.0,
                "bearish": -3.0,
                "pump": 2.5,
                "dump": -2.5,
                "rekt": -3.5,
                "hodl": 2.0,
                "fud": -2.5,
                "fomo": 1.5,
                "dip": -1.5,
                "rally": 2.5,
                "crash": -3.5,
                "surge": 3.0,
                "plunge": -3.0,
                "breakthrough": 2.5,
                "adoption": 2.0,
                "regulation": -1.0,
                "ban": -3.0,
                "partnership": 2.5,
                "hack": -3.5,
                "scam": -4.0,
                "innovation": 2.5,
                "institutional": 2.0,
                "whale": 0.5,
            }
            self.vader.lexicon.update(crypto_lexicon_updates)
            
            logger.info("✅ Sentiment analyzer initialized with crypto lexicon")
        except Exception as e:
            logger.error(f"❌ Failed to initialize sentiment analyzer: {e}")
            raise
    
    def analyze_vader(self, text: str) -> Dict[str, float]:
        """
        Analyze sentiment using VADER
        
        Args:
            text: Text to analyze
        
        Returns:
            Dictionary with sentiment scores
        """
        scores = self.vader.polarity_scores(text)
        return {
            "positive": scores["pos"],
            "negative": scores["neg"],
            "neutral": scores["neu"],
            "compound": scores["compound"],  # -1 to 1
        }
    
    def analyze_textblob(self, text: str) -> Dict[str, float]:
        """
        Analyze sentiment using TextBlob
        
        Args:
            text: Text to analyze
        
        Returns:
            Dictionary with sentiment scores
        """
        blob = TextBlob(text)
        return {
            "polarity": blob.sentiment.polarity,  # -1 to 1
            "subjectivity": blob.sentiment.subjectivity,  # 0 to 1
        }
    
    def analyze(self, text: str) -> Dict[str, any]:
        """
        Perform comprehensive sentiment analysis
        
        Args:
            text: Text to analyze
        
        Returns:
            Combined sentiment analysis results
        """
        if not text or len(text.strip()) == 0:
            return {
                "score": 0.0,
                "magnitude": 0.0,
                "sentiment": "neutral",
                "confidence": 0.0,
            }
        
        try:
            # Get scores from both analyzers
            vader_scores = self.analyze_vader(text)
            textblob_scores = self.analyze_textblob(text)
            
            # Combine scores (weighted average)
            compound_score = (
                vader_scores["compound"] * 0.7 +  # VADER weighted more for social
                textblob_scores["polarity"] * 0.3
            )
            
            # Calculate magnitude (strength of sentiment)
            magnitude = abs(compound_score)
            
            # Determine sentiment category
            if compound_score >= 0.05:
                sentiment = "bullish"
            elif compound_score <= -0.05:
                sentiment = "bearish"
            else:
                sentiment = "neutral"
            
            # Calculate confidence based on agreement between analyzers
            agreement = 1.0 - abs(vader_scores["compound"] - textblob_scores["polarity"]) / 2.0
            confidence = min(magnitude * agreement, 1.0)
            
            return {
                "score": round(compound_score, 4),  # -1.0 to 1.0
                "magnitude": round(magnitude, 4),  # 0.0 to 1.0
                "sentiment": sentiment,
                "confidence": round(confidence, 4),  # 0.0 to 1.0
                "vader": vader_scores,
                "textblob": textblob_scores,
            }
        except Exception as e:
            logger.error(f"Error analyzing sentiment: {e}")
            return {
                "score": 0.0,
                "magnitude": 0.0,
                "sentiment": "neutral",
                "confidence": 0.0,
                "error": str(e),
            }
    
    def analyze_batch(self, texts: List[str]) -> List[Dict[str, any]]:
        """
        Analyze multiple texts
        
        Args:
            texts: List of texts to analyze
        
        Returns:
            List of sentiment analysis results
        """
        return [self.analyze(text) for text in texts]
    
    def aggregate_sentiment(
        self,
        analyses: List[Dict[str, any]],
        weights: Optional[List[float]] = None
    ) -> Dict[str, float]:
        """
        Aggregate multiple sentiment analyses
        
        Args:
            analyses: List of sentiment analysis results
            weights: Optional weights for each analysis (must sum to 1.0)
        
        Returns:
            Aggregated sentiment score
        """
        if not analyses:
            return {"score": 0.0, "magnitude": 0.0, "confidence": 0.0}
        
        # Default to equal weights
        if weights is None:
            weights = [1.0 / len(analyses)] * len(analyses)
        
        # Validate weights
        if len(weights) != len(analyses) or abs(sum(weights) - 1.0) > 0.01:
            logger.warning("Invalid weights provided, using equal weights")
            weights = [1.0 / len(analyses)] * len(analyses)
        
        # Calculate weighted average
        weighted_score = sum(a["score"] * w for a, w in zip(analyses, weights))
        weighted_magnitude = sum(a["magnitude"] * w for a, w in zip(analyses, weights))
        weighted_confidence = sum(a["confidence"] * w for a, w in zip(analyses, weights))
        
        return {
            "score": round(weighted_score, 4),
            "magnitude": round(weighted_magnitude, 4),
            "confidence": round(weighted_confidence, 4),
            "count": len(analyses),
        }


# Global sentiment analyzer instance
sentiment_analyzer = SentimentAnalyzer()
