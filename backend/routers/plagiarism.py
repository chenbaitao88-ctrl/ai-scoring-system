"""
抄袭检测API
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db, Work
from services.plagiarism_detector import PlagiarismDetector

router = APIRouter()


@router.post("/detect")
async def detect_plagiarism(
    language: str = "Python",
    db: Session = Depends(get_db)
):
    """对所有作品进行抄袭检测"""
    detector = PlagiarismDetector(db)
    report = detector.detect_all(language)

    return {
        "message": "抄袭检测完成",
        "total_pairs": report.total_pairs,
        "suspicious_pairs": report.suspicious_pairs,
        "high_risk": report.high_risk,
        "medium_risk": report.medium_risk,
        "low_risk": report.low_risk,
        "results": [
            {
                "team1": {
                    "id": r.team1_id,
                    "name": r.team1_name,
                },
                "team2": {
                    "id": r.team2_id,
                    "name": r.team2_name,
                },
                "token_similarity": round(r.token_similarity, 3),
                "structure_similarity": round(r.structure_similarity, 3),
                "overall_similarity": round(r.overall_similarity, 3),
                "suspicion_level": r.suspicion_level,
            }
            for r in sorted(report.results, key=lambda x: x.overall_similarity, reverse=True)
            if r.is_suspicious
        ],
        "team_suspicion_scores": {
            str(k): round(v, 3) for k, v in report.team_suspicion_scores.items()
        }
    }


@router.get("/suspicious")
async def get_suspicious_teams(db: Session = Depends(get_db)):
    """获取有抄袭嫌疑的队伍列表"""
    detector = PlagiarismDetector(db)
    return {"teams": detector.get_suspicious_teams()}


@router.post("/clear-flags")
async def clear_plagiarism_flags(db: Session = Depends(get_db)):
    """清除所有抄袭标记"""
    works = db.query(Work).filter(Work.plagiarism_flag == True).all()
    for work in works:
        work.plagiarism_flag = False
        work.flag_reason = None
    db.commit()
    return {"message": f"已清除 {len(works)} 个抄袭标记"}
