import os
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, timedelta

# Load environment variables
load_dotenv()

# Initialize FastAPI
app = FastAPI()

# Enable CORS so your frontend dashboard can talk to this API without browser errors
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins (update to your frontend URL in production)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase Admin Client
supabase_url: str = os.environ.get("SUPABASE_URL")
supabase_key: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def add_working_days(start_date: datetime, days_to_add: int) -> datetime:
    """Adds days to a date, skipping Saturdays (5) and Sundays (6)."""
    current_date = start_date
    added_days = 0
    while added_days < days_to_add:
        current_date += timedelta(days=1)
        if current_date.weekday() < 5:
            added_days += 1
    return current_date

def log_audit(staff_id: str, action_type: str, description: str):
    """Silently logs administrative actions for accountability."""
    try:
        supabase.table("audit_logs").insert({
            "staff_id": staff_id,
            "action_type": action_type,
            "description": description
        }).execute()
    except Exception as e:
        print(f"Audit log failed (Non-blocking): {str(e)}")

# ==========================================
# PYDANTIC MODELS
# ==========================================
class StaffRequest(BaseModel):
    fullName: str
    email: str
    password: str
    department: str
    role_level: str

class GradeEntry(BaseModel):
    student_name: str
    admission_number: str
    cat_score: float
    exam_score: float
    is_dns: Optional[bool] = False # Added DNS flag

class GradeSubmission(BaseModel):
    block_name: str
    unit_name: str
    lecturer_id: str
    grades: List[GradeEntry]

class ApprovalRequest(BaseModel):
    block_name: str
    unit_name: str
    action: str # "Approve" or "Reject"
    staff_id: str # Added to track who approved it in the Audit Logs

class AnnouncementRequest(BaseModel):
    title: str
    message: str
    staff_id: str
    target_audience: Optional[str] = "All Students" # Added to match your database schema securely

# ==========================================
# ENDPOINTS
# ==========================================
@app.post("/api/create-staff")
async def create_staff(request: StaffRequest):
    try:
        # 1. Create the secure user in Supabase Auth
        auth_response = supabase.auth.admin.create_user({
            "email": request.email,
            "password": request.password,
            "email_confirm": True
        })
        
        user_id = auth_response.user.id

        # 2. Map the data for the database insertion
        profile_data = {
            "auth_id": user_id,
            "full_name": request.fullName,
            "email": request.email,
            "department": request.department,
            "role_level": request.role_level,
            "is_active": True
        }

        # 3. Insert the permissions profile into your table
        supabase.table("staff_profiles").insert(profile_data).execute()

        return {"success": True, "userId": user_id}

    except Exception as e:
        print(f"Error creating staff: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/submit-grades")
async def submit_grades(request: GradeSubmission):
    try:
        # --- SERVER-SIDE 100% MATH GATEWAY VALIDATION ---
        # 1. Fetch expected ungraded students for this block and unit
        students_res = supabase.table("students").select("admission_number").eq("block", request.block_name).eq("is_approved", True).execute()
        all_students_in_block = [row['admission_number'] for row in students_res.data]
        
        grades_res = supabase.table("exam_results").select("admission_number").eq("block_name", request.block_name).eq("unit_name", request.unit_name).execute()
        already_graded = [row['admission_number'] for row in grades_res.data]
        
        expected_ungraded = [adm for adm in all_students_in_block if adm not in already_graded]
        submitted_admissions = [entry.admission_number for entry in request.grades]
        
        # Verify every expected student is in the submission
        missing = [adm for adm in expected_ungraded if adm not in submitted_admissions]
        if missing:
            raise ValueError(f"100% Math Gateway Failed: Missing results for {len(missing)} expected student(s). All students must be accounted for or marked as DNS.")
        # -------------------------------------------------

        payload = []
        for entry in request.grades:
            # Check for DNS
            if entry.is_dns:
                total = 0.0
                grade_label = "DNS"
            else:
                # 1. Python calculates the total securely on the server
                total = entry.cat_score + entry.exam_score
                
                # 2. Python determines the grade securely
                if total >= 80:
                    grade_label = "Distinction"
                elif total >= 70:
                    grade_label = "Credit"
                elif total >= 60:
                    grade_label = "Pass"
                else:
                    grade_label = "Fail"

            payload.append({
                "student_name": entry.student_name,
                "admission_number": entry.admission_number,
                "block_name": request.block_name,
                "unit_name": request.unit_name,
                "lecturer_id": request.lecturer_id,
                "cat_score": entry.cat_score,
                "exam_score": entry.exam_score,
                "total_score": total,
                "grade": grade_label,
                "status": "Pending" # Locks it until HOD approves
            })
        
        # 3. Insert securely via Service Role
        supabase.table("exam_results").insert(payload).execute()
        
        # 4. Log the action
        log_audit(request.lecturer_id, "SUBMIT_GRADES", f"Submitted {len(payload)} grades for {request.block_name} - {request.unit_name}")

        return {"success": True, "message": f"Successfully submitted {len(payload)} grades to HOD."}

    except Exception as e:
        print(f"Server Error: {str(e)}")
        # Send the exact error message back to the frontend toast notification
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/approve-results")
async def approve_results(request: ApprovalRequest):
    try:
        final_status = "Approved" if request.action == "Approve" else "Rejected"
        
        # 1. Update the database securely
        supabase.table("exam_results").update({"status": final_status})\
            .eq("block_name", request.block_name)\
            .eq("unit_name", request.unit_name)\
            .eq("status", "Pending").execute()
            
        log_audit(request.staff_id, f"BLOCK_{request.action.upper()}", f"{request.action}d {request.unit_name} for {request.block_name}")

        # 2. DYNAMIC "All-or-Nothing" Publish Logic
        if final_status == "Approved":
            
            # A. Find out how many unique units the lecturers actually registered for this block
            assigned_res = supabase.table("unit_assignments").select("unit_name").eq("block_name", request.block_name).execute()
            
            if assigned_res.data:
                # Count the unique units assigned to this block
                required_units = len(set(row['unit_name'] for row in assigned_res.data))
                
                # B. Find out how many units have been approved by the HOD so far
                approved_res = supabase.table("exam_results").select("unit_name").eq("block_name", request.block_name).eq("status", "Approved").execute()
                approved_units_count = len(set(row['unit_name'] for row in approved_res.data))
                
                # C. If the approved count matches the registered count, unlock the student dashboards!
                if required_units > 0 and approved_units_count >= required_units:
                    # THE FIX: We use UPSERT to auto-create the block status if it doesn't exist
                    supabase.table("student_blocks").upsert({
                        "block_name": request.block_name, 
                        "is_published": True
                    }).execute()
                    
                    log_audit(request.staff_id, "PUBLISH_BLOCK", f"Automatically published {request.block_name} to student portal.")

        return {"success": True, "status": final_status}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/ungraded-students/{block_name}/{unit_name}")
async def get_ungraded_students(block_name: str, unit_name: str):
    """Fetches ONLY approved students in a block who have NOT yet received a grade for a specific unit."""
    try:
        # 1. Fetch all APPROVED students in this block
        students_res = (
            supabase.table("students")
            .select("first_name, last_name, admission_number")
            .eq("block", block_name)
            .eq("is_approved", True)
            .execute()
        )
        all_students = students_res.data
        
        # 2. Fetch all students who already have a grade entry for this unit
        grades_res = supabase.table("exam_results").select("admission_number").eq("block_name", block_name).eq("unit_name", unit_name).execute()
        graded_admissions = [row['admission_number'] for row in grades_res.data]
        
        # 3. Filter the list and format the name so the frontend receives what it expects
        ungraded_students = []
        for s in all_students:
            if s['admission_number'] not in graded_admissions:
                # Combine first and last name natively in Python
                full_name = f"{s.get('first_name', '')} {s.get('last_name', '')}".strip()
                s['student_name'] = full_name
                ungraded_students.append(s)
        
        return {"success": True, "students": ungraded_students}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/create-announcement")
async def create_announcement(request: AnnouncementRequest):
    """Creates an announcement securely mapped to the global_announcements table."""
    try:
        # Directly mapping to your actual database columns. 
        # We removed Python's naive datetime and let Supabase handle created_at and is_active natively to prevent 400 errors.
        payload = {
            "title": request.title,
            "message": request.message,
            "target_audience": request.target_audience,
            "posted_by": request.staff_id
        }
        
        supabase.table("global_announcements").insert(payload).execute()
        log_audit(request.staff_id, "POST_ANNOUNCEMENT", f"Posted: {request.title}")
        
        return {"success": True, "message": "Announcement broadcasted successfully."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
