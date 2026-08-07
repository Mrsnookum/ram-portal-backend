import os
from typing import List, Optional, Dict, Any
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

class UpdateStaffRequest(BaseModel):
    staff_id: str # Profile UUID
    requester_id: str # Auth ID of the person making the change
    full_name: str
    email: str
    department: str
    role_level: str
    is_active: bool

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
    edited_grades: Optional[List[Dict[str, Any]]] = None # Captures any HOD edits made during the review process

class AnnouncementRequest(BaseModel):
    title: str
    message: str
    staff_id: str
    target_audience: Optional[str] = "All Students" # Added to match your database schema securely

class EditAnnouncementRequest(BaseModel):
    announcement_id: str
    title: str
    message: str
    target_audience: str
    requester_id: str

class DeleteAnnouncementRequest(BaseModel):
    announcement_id: str
    requester_id: str

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


@app.post("/api/update-staff")
async def update_staff(request: UpdateStaffRequest):
    """Securely updates a staff member's profile and physically revokes their Auth token if disabled."""
    try:
        # 1. Verify the requester has authorization (SuperAdmin, Principal, or HOD managing their own dept)
        req_profile = supabase.table("staff_profiles").select("role_level").eq("auth_id", request.requester_id).single().execute()
        req_role = req_profile.data.get('role_level')
        
        if req_role not in ["SuperAdmin", "Principal", "HOD", "Deputy HOD"]:
            raise PermissionError("You are not authorized to modify staff accounts.")

        # 2. Update the public staff profile table
        supabase.table("staff_profiles").update({
            "full_name": request.full_name,
            "email": request.email,
            "department": request.department,
            "role_level": request.role_level,
            "is_active": request.is_active
        }).eq("id", request.staff_id).execute()

        # 3. SECURE AUTH SUSPENSION: Grab their underlying auth_id and ban them
        staff_res = supabase.table("staff_profiles").select("auth_id").eq("id", request.staff_id).single().execute()
        if staff_res.data and staff_res.data.get("auth_id"):
            auth_id = staff_res.data["auth_id"]
            # Supabase Admin trick to physically lock an account: set a ban duration
            ban_time = "none" if request.is_active else "876000h" # 100 years suspension if deactivated
            supabase.auth.admin.update_user_by_id(auth_id, {"ban_duration": ban_time})

        # 4. Log the action
        status_text = "Reactivated" if request.is_active else "Deactivated/Suspended"
        log_audit(request.requester_id, "UPDATE_STAFF", f"{status_text} staff profile and updated details for {request.full_name}")

        return {"success": True, "message": "Staff profile securely updated."}
    except Exception as e:
        print(f"Update Staff Error: {str(e)}")
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
        
        # --- INCORPORATE THE HOD OVERWRITES IF ANY EXIST ---
        if request.edited_grades and final_status == "Approved":
            for grade_entry in request.edited_grades:
                # Calculate new label safely on the server
                exam_score = float(grade_entry.get('exam_score', 0))
                is_dns = grade_entry.get('is_dns', False)
                
                if is_dns:
                    total = 0.0
                    grade_label = "DNS"
                else:
                    total = exam_score
                    if total >= 80: grade_label = "Distinction"
                    elif total >= 70: grade_label = "Credit"
                    elif total >= 60: grade_label = "Pass"
                    else: grade_label = "Fail"
                    
                # Update individually
                supabase.table("exam_results").update({
                    "exam_score": total,
                    "total_score": total,
                    "grade": grade_label
                }).eq("block_name", request.block_name)\
                  .eq("unit_name", request.unit_name)\
                  .eq("admission_number", grade_entry.get('admission_number')).execute()
        # ----------------------------------------------------

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


@app.post("/api/edit-announcement")
async def edit_announcement(request: EditAnnouncementRequest):
    """Edits an announcement ONLY if the requester is the owner or an executive admin."""
    try:
        ann_res = supabase.table("global_announcements").select("posted_by").eq("id", request.announcement_id).single().execute()
        if not ann_res.data:
            raise ValueError("Announcement not found.")
        
        posted_by = ann_res.data.get("posted_by")
        
        req_profile = supabase.table("staff_profiles").select("role_level").eq("auth_id", request.requester_id).single().execute()
        req_role = req_profile.data.get("role_level")

        if posted_by != request.requester_id and req_role not in ["SuperAdmin", "Principal", "Principal / Deputy"]:
            raise PermissionError("Unauthorized to edit this announcement. You are not the owner.")

        supabase.table("global_announcements").update({
            "title": request.title,
            "message": request.message,
            "target_audience": request.target_audience
        }).eq("id", request.announcement_id).execute()

        log_audit(request.requester_id, "EDIT_ANNOUNCEMENT", f"Edited announcement ID: {request.announcement_id}")
        return {"success": True, "message": "Announcement securely updated."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/delete-announcement")
async def delete_announcement(request: DeleteAnnouncementRequest):
    """Soft deletes an announcement ONLY if the requester is the owner or an executive admin."""
    try:
        ann_res = supabase.table("global_announcements").select("posted_by").eq("id", request.announcement_id).single().execute()
        if not ann_res.data:
            raise ValueError("Announcement not found.")
        
        posted_by = ann_res.data.get("posted_by")

        req_profile = supabase.table("staff_profiles").select("role_level").eq("auth_id", request.requester_id).single().execute()
        req_role = req_profile.data.get("role_level")

        if posted_by != request.requester_id and req_role not in ["SuperAdmin", "Principal", "Principal / Deputy"]:
            raise PermissionError("Unauthorized to delete this announcement. You are not the owner.")

        supabase.table("global_announcements").update({
            "is_active": False
        }).eq("id", request.announcement_id).execute()

        log_audit(request.requester_id, "DELETE_ANNOUNCEMENT", f"Soft-deleted announcement ID: {request.announcement_id}")
        return {"success": True, "message": "Announcement securely removed."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/audit-logs")
async def get_audit_logs(requester_id: str, filter_type: str = "ALL"):
    """Securely fetches audit logs by manually joining tables in Python to bypass strict Foreign Key requirements."""
    try:
        # 1. Verify access
        req_profile = supabase.table("staff_profiles").select("role_level").eq("auth_id", requester_id).single().execute()
        if not req_profile.data:
            raise PermissionError("Requester profile not found.")
            
        req_role = req_profile.data.get("role_level")

        if req_role not in ["SuperAdmin", "Principal", "Principal / Deputy"]:
            raise PermissionError("Unauthorized access. SuperAdmin or Principal role required to view audit logs.")

        # 2. Fetch the raw audit logs first (WITHOUT the relational join)
        query = supabase.table("audit_logs").select("*").order("created_at", desc=True).limit(100)

        if filter_type == "AUTH":
            query = query.in_("action_type", ["UPDATE_STAFF", "CREATE_STAFF"])
        elif filter_type == "GRADES":
            query = query.in_("action_type", ["SUBMIT_GRADES", "BLOCK_APPROVE", "BLOCK_REJECT", "PUBLISH_BLOCK"])
        elif filter_type == "ANNOUNCEMENTS":
            query = query.in_("action_type", ["POST_ANNOUNCEMENT", "EDIT_ANNOUNCEMENT", "DELETE_ANNOUNCEMENT"])

        res = query.execute()
        logs = res.data
        
        if not logs:
            return {"success": True, "logs": []}

        # 3. Fetch all staff profiles and create a lookup dictionary
        profiles_res = supabase.table("staff_profiles").select("id, auth_id, full_name, role_level").execute()
        profiles_map = {}
        for p in profiles_res.data:
            # We map both 'id' and 'auth_id' to cover our bases, since different API routes might log different IDs
            profiles_map[str(p["id"])] = {"full_name": p["full_name"], "role_level": p["role_level"]}
            if p.get("auth_id"):
                profiles_map[str(p["auth_id"])] = {"full_name": p["full_name"], "role_level": p["role_level"]}
                
        # 4. Manually stitch the profile data into the logs
        for log in logs:
            staff_id = str(log.get("staff_id", ""))
            if staff_id in profiles_map:
                log["staff_profiles"] = profiles_map[staff_id]
            else:
                # Fallback if the staff member was deleted from the database entirely
                log["staff_profiles"] = {"full_name": "System / Unknown", "role_level": "Admin"}
        
        return {"success": True, "logs": logs}
        
    except Exception as e:
        print(f"Audit Logs Fetch Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
