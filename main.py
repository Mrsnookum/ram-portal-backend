import os
import requests
import secrets
import uuid
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

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

def dispatch_bulk_notifications(target_audience: str, subject: str, message_text: str):
    """Silently dispatches notifications via Telegram and WhatsApp in the background."""
    try:
        # 1. Fetch target students based on audience
        if target_audience == "All Students":
            res = supabase.table("students").select("telegram_chat_id, whatsapp_phone").execute()
        else:
            res = supabase.table("students").select("telegram_chat_id, whatsapp_phone").eq("block", target_audience).execute()
            
        students = res.data if res.data else []
        if not students:
            return
            
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        node_url = os.environ.get("WHATSAPP_NODE_URL")
        
        for student in students:
            # Dispatch Telegram Ping
            if student.get("telegram_chat_id") and bot_token:
                try:
                    requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={
                        "chat_id": student["telegram_chat_id"],
                        "text": f"📢 {subject}\n\n{message_text}"
                    }, timeout=5)
                except:
                    pass
                    
            # Dispatch WhatsApp Ping
            if student.get("whatsapp_phone") and node_url:
                try:
                    requests.post(node_url, json={
                        "phone": student["whatsapp_phone"],
                        "message": f"📢 *{subject}*\n\n{message_text}"
                    }, timeout=60)
                except:
                    pass
                    
    except Exception as e:
        print(f"Background notification error: {e}")


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

class PromoteRequest(BaseModel):
    current_block: str
    students: List[str]
    requester_id: str

# NEW: OTP Password Reset Models
class OtpRequest(BaseModel):
    admission_number: str
    email: str

class OtpVerify(BaseModel):
    admission_number: str
    otp_code: str

class PasswordReset(BaseModel):
    admission_number: str
    otp_code: str
    new_password: str

# NEW: Notification Linking Models
class TelegramLinkRequest(BaseModel):
    admission_number: str

class WhatsAppLinkRequest(BaseModel):
    admission_number: str
    phone_number: str

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
        # 1. Verify the requester has authorization
        req_profile = supabase.table("staff_profiles").select("role_level").eq("auth_id", request.requester_id).single().execute()
        req_role = req_profile.data.get('role_level')
        
        if req_role not in ["SuperAdmin", "Principal", "Principal / Deputy", "HOD", "Deputy HOD"]:
            raise PermissionError("You are not authorized to modify staff accounts.")

        # --- EXECUTIVE LOCK / SINGLETON ROLES ---
        # Prevent non-SuperAdmins from assigning or modifying executive roles
        restricted_roles = ["SuperAdmin", "Principal", "Principal / Deputy", "QA Officer", "HOD"]
        
        # Check the target's current role in the database
        target_res = supabase.table("staff_profiles").select("role_level").eq("id", request.staff_id).single().execute()
        target_current_role = target_res.data.get("role_level") if target_res.data else ""

        if req_role != "SuperAdmin":
            if request.role_level in restricted_roles or target_current_role in restricted_roles:
                raise PermissionError("Security Lock: Only the SuperAdmin can modify or assign executive roles.")
        # ----------------------------------------

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
async def approve_results(request: ApprovalRequest, background_tasks: BackgroundTasks):
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

        # 2. DYNAMIC "All-or-Nothing" Publish Logic & AUTO-SUPPLEMENTARIES
        if final_status == "Approved":
            
            # --- AUTO-SUPPLEMENTARY GENERATOR ---
            # Find the students who just failed or got DNS in this approval batch
            approved_res = supabase.table("exam_results").select("*")\
                .eq("block_name", request.block_name)\
                .eq("unit_name", request.unit_name)\
                .eq("status", "Approved").execute()
                
            if approved_res.data:
                supp_payload = []
                for row in approved_res.data:
                    # If the grade was Fail or DNS, auto-generate the supplementary row
                    if row.get("grade") in ["Fail", "DNS"]:
                        # Prevent infinite duplicates if they approve multiple times
                        check_existing = supabase.table("exam_results").select("id")\
                            .eq("admission_number", row.get("admission_number"))\
                            .eq("block_name", request.block_name)\
                            .eq("unit_name", request.unit_name)\
                            .eq("is_supplementary", True)\
                            .eq("status", "Pending").execute()
                            
                        if not check_existing.data:
                            supp_payload.append({
                                "student_name": row.get("student_name"),
                                "admission_number": row.get("admission_number"),
                                "block_name": request.block_name,
                                "unit_name": request.unit_name,
                                "lecturer_id": row.get("lecturer_id"),
                                "cat_score": 0.0,
                                "exam_score": 0.0,
                                "total_score": 0.0,
                                "grade": "Pending", 
                                "status": "Pending", 
                                "is_supplementary": True
                            })
                            
                if supp_payload:
                    supabase.table("exam_results").insert(supp_payload).execute()
                    log_audit(request.staff_id, "AUTO_SUPPLEMENTARY", f"Auto-generated {len(supp_payload)} supplementary records for {request.unit_name}")
            # ------------------------------------

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
                    
                    # TRIGGER BACKGROUND NOTIFICATIONS FOR RESULTS
                    alert_subject = "EXAM RESULTS PUBLISHED!"
                    alert_body = f"All units for {request.block_name} have been officially approved and published. Log in to your student portal to view your grades."
                    background_tasks.add_task(dispatch_bulk_notifications, request.block_name, alert_subject, alert_body)

        return {"success": True, "status": final_status}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/promote-students")
async def promote_students(request: PromoteRequest):
    """Processes mass promotion of cleared students to their next academic block."""
    try:
        # 1. Verify access
        req_profile = supabase.table("staff_profiles").select("role_level").eq("auth_id", request.requester_id).single().execute()
        if not req_profile.data:
            raise PermissionError("Requester profile not found.")
            
        req_role = req_profile.data.get("role_level")

        if req_role not in ["SuperAdmin", "Principal", "Principal / Deputy", "HOD", "Deputy HOD"]:
            raise PermissionError("Unauthorized access. Admin role required to promote students.")

        # 2. Progression Map
        progression_map = {
            "Introductory": "Block 1",
            "Block 1": "Block 2",
            "Block 2": "Block 3/4",
            "Block 3/4": "Block 5",
            "Block 5": "Alumni / Completed"
        }
        
        current = request.current_block
        next_block = progression_map.get(current)
        
        if not next_block:
            raise ValueError(f"Invalid progression block or end of progression reached for '{current}'.")
            
        # 3. Batch Update Students
        supabase.table("students").update({
            "block": next_block
        }).in_("admission_number", request.students).eq("block", current).execute()
        
        log_audit(request.requester_id, "PROMOTE_COHORT", f"Promoted {len(request.students)} students from {current} to {next_block}")
        
        return {"success": True, "message": f"Successfully promoted {len(request.students)} students to {next_block}."}
        
    except Exception as e:
        print(f"Promotion Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/ungraded-students")
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
async def create_announcement(request: AnnouncementRequest, background_tasks: BackgroundTasks):
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
        
        # TRIGGER BACKGROUND NOTIFICATIONS FOR ANNOUNCEMENTS
        alert_subject = f"NEW ANNOUNCEMENT: {request.title}"
        alert_body = f"{request.message}\n\nLog in to the RAM Portal to view details."
        background_tasks.add_task(dispatch_bulk_notifications, request.target_audience, alert_subject, alert_body)
        
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

        if posted_by != request.requester_id and req_role not in ["SuperAdmin", "Principal", "Principal / Deputy", "QA Officer"]:
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

        if posted_by != request.requester_id and req_role not in ["SuperAdmin", "Principal", "Principal / Deputy", "QA Officer"]:
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

        if req_role not in ["SuperAdmin", "Principal", "Principal / Deputy", "QA Officer"]:
            raise PermissionError("Unauthorized access. SuperAdmin, Principal or QA role required to view audit logs.")

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
                
        # 4. Manually stitch the profile data into the logs & Apply Ghost Protocol
        final_logs = []
        for log in logs:
            staff_id = str(log.get("staff_id", ""))
            if staff_id in profiles_map:
                profile = profiles_map[staff_id]
            else:
                # Fallback if the staff member was deleted from the database entirely
                profile = {"full_name": "System / Unknown", "role_level": "Admin"}
                
            # --- GHOST SUPERADMIN PROTOCOL ---
            # If the person who performed the action was a SuperAdmin, 
            # and the person viewing the logs is NOT a SuperAdmin, hide the log entirely.
            if req_role != "SuperAdmin" and profile.get("role_level") == "SuperAdmin":
                continue
            # ---------------------------------
            
            log["staff_profiles"] = profile
            final_logs.append(log)
        
        return {"success": True, "logs": final_logs}
        
    except Exception as e:
        print(f"Audit Logs Fetch Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/request-otp")
async def request_otp(request: OtpRequest):
    """Step 1: Validate Student, Generate 6-Digit OTP, and Email via Brevo"""
    try:
        # 1. Verify student exists in the database
        student_res = supabase.table("students").select("auth_id, first_name").eq("admission_number", request.admission_number).execute()
        if not student_res.data:
            raise ValueError("Admission number not found in our records.")
            
        student_record = student_res.data[0]
        auth_id = student_record.get("auth_id")
        first_name = student_record.get("first_name", "Student")
        
        # 2. Verify email strictly matches their registered 'contact_email' in Auth Metadata
        user_res = supabase.auth.admin.get_user_by_id(auth_id)
        if not user_res.user:
            raise ValueError("Authentication record could not be found.")
            
        metadata = user_res.user.user_metadata or {}
        registered_email = metadata.get("contact_email")
        
        if not registered_email or registered_email.lower() != request.email.lower():
            raise ValueError("The provided email does not match our secure records for this admission number.")
            
        # 3. Generate a secure 6-digit OTP
        otp_code = "".join([str(secrets.randbelow(10)) for _ in range(6)])
        
        # Set expiration to 10 minutes securely using UTC Timezone
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        
        # 4. Save to the database safely using Service Role (bypassing RLS visibility for frontend users)
        supabase.table("password_resets").insert({
            "admission_number": request.admission_number,
            "email": request.email,
            "otp_code": otp_code,
            "expires_at": expires_at,
            "is_used": False
        }).execute()
        
        # 5. Send via Brevo API using environment variables
        brevo_url = "https://api.brevo.com/v3/smtp/email"
        brevo_api_key = os.environ.get("BREVO_API_KEY")
        brevo_sender_email = os.environ.get("BREVO_SENDER_EMAIL")
        
        if not brevo_api_key or not brevo_sender_email:
            raise ValueError("Email server configuration is missing. Contact Admin.")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "api-key": brevo_api_key
        }
        
        # Beautiful HTML email directly generated by Python
        payload = {
            "sender": {"email": brevo_sender_email, "name": "RAM Portal Administration"},
            "to": [{"email": request.email, "name": first_name}],
            "subject": "RAM Portal - Secure Password Reset Code",
            "htmlContent": f"""
            <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px; border: 1px solid #eaeaea; border-radius: 12px; background-color: #ffffff;">
                <h2 style="color: #003366; text-align: center; border-bottom: 2px solid #D4AF37; padding-bottom: 10px;">RAM Training College</h2>
                <p style="color: #333333;">Hello <strong>{first_name}</strong>,</p>
                <p style="color: #555555;">We received a request to reset the password for your student portal account.</p>
                
                <div style="background-color: #f8fafc; padding: 20px; text-align: center; border-radius: 8px; margin: 25px 0; border: 1px dashed #cbd5e1;">
                    <p style="margin: 0; color: #64748b; font-size: 12px; text-transform: uppercase; font-weight: bold; margin-bottom: 8px;">Your Verification Code</p>
                    <span style="font-size: 32px; font-weight: 900; letter-spacing: 8px; color: #003366;">{otp_code}</span>
                </div>
                
                <p style="color: #555555;">Enter this 6-digit code in the portal to securely reset your password. <br><strong>This code expires in 10 minutes.</strong></p>
                <p style="font-size: 11px; color: #94a3b8; text-align: center; margin-top: 40px; border-top: 1px solid #f1f5f9; padding-top: 15px;">If you did not request this code, please ignore this email. Your account remains secure.</p>
            </div>
            """
        }
        
        resp = requests.post(brevo_url, json=payload, headers=headers)
        if resp.status_code not in [200, 201, 202]:
            brevo_error = resp.json()
            print(f"BREVO API ERROR: {brevo_error}")
            raise ValueError(f"Email failed to send: {brevo_error.get('message', 'Unknown Error')}")
            
        return {"success": True, "message": "Verification code dispatched securely."}
        
    except Exception as e:
        print(f"Request OTP Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/verify-otp")
async def verify_otp(request: OtpVerify):
    """Step 2: Verify the provided OTP code hasn't expired or been used"""
    try:
        # Check latest unused OTP entry for this admission number
        res = supabase.table("password_resets").select("*")\
            .eq("admission_number", request.admission_number)\
            .eq("otp_code", request.otp_code)\
            .eq("is_used", False)\
            .execute()
            
        if not res.data:
            raise ValueError("Invalid code. Please check your email and try again.")
            
        record = res.data[-1]
        
        # Safely parse ISO format and compare UTC times
        expires_at = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
        
        if datetime.now(timezone.utc) > expires_at:
            raise ValueError("This security code has expired. Please request a new one.")
            
        return {"success": True, "message": "Identity verified successfully."}
        
    except Exception as e:
        print(f"Verify OTP Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/reset-password")
async def reset_password(request: PasswordReset):
    """Step 3: Burn the OTP and forcefully update the Auth Password"""
    try:
        # 1. Final rigorous verification of the OTP session
        res = supabase.table("password_resets").select("*")\
            .eq("admission_number", request.admission_number)\
            .eq("otp_code", request.otp_code)\
            .eq("is_used", False)\
            .execute()
            
        if not res.data:
            raise ValueError("Invalid or compromised session.")
            
        record = res.data[-1]
        expires_at = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            raise ValueError("This session has expired. Please start over.")
            
        # 2. Get the student's internal auth_id
        student_res = supabase.table("students").select("auth_id").eq("admission_number", request.admission_number).execute()
        if not student_res.data:
            raise ValueError("Student profile verification failed.")
        auth_id = student_res.data[0]["auth_id"]
        
        # 3. Update password directly via Supabase Auth Admin Service Role (Bypassing normal Auth flow)
        supabase.auth.admin.update_user_by_id(auth_id, {"password": request.new_password})
        
        # 4. Burn the OTP immediately so it can never be used again
        supabase.table("password_resets").update({"is_used": True}).eq("id", record["id"]).execute()
        
        return {"success": True, "message": "Password successfully updated."}
        
    except Exception as e:
        print(f"Reset Password Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

# ==========================================
# NOTIFICATION & MESSAGING ENDPOINTS
# ==========================================
@app.get("/api/notification-status")
async def check_status(admission_number: str):
    try:
        res = supabase.table("students").select("telegram_chat_id, whatsapp_phone").eq("admission_number", admission_number).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Student not found.")
        
        student = res.data[0]
        return {
            "telegram_linked": bool(student.get("telegram_chat_id")),
            "whatsapp_linked": bool(student.get("whatsapp_phone"))
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/generate-telegram-link")
async def generate_tg_link(request: TelegramLinkRequest):
    try:
        token = str(uuid.uuid4())
        
        # Save the temporary token to the student's profile securely
        supabase.table("students").update({"auth_link_token": token}).eq("admission_number", request.admission_number).execute()
        
        bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "YourCollegeBot")
        return {"link": f"https://t.me/{bot_username}?start={token}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/link-whatsapp")
async def link_whatsapp(request: WhatsAppLinkRequest):
    try:
        phone = request.phone_number.strip().replace("+", "")
        
        # Auto-format to Kenyan code if they typed 07...
        if phone.startswith("0"):
            phone = "254" + phone[1:]
            
        # 1. Save to Supabase database
        supabase.table("students").update({"whatsapp_phone": phone}).eq("admission_number", request.admission_number).execute()
        
        # 2. Trigger the WhatsApp Node.js Microservice to send a welcome message
        node_url = os.environ.get("WHATSAPP_NODE_URL")
        
        if node_url:
            payload = {
                "phone": phone,
                "message": "✅ Success! Your WhatsApp is now securely linked to the RAM Portal for instant notifications."
            }
            
            # Fire the message and wait up to 60s for Node.js to wake up
            try:
                requests.post(node_url, json=payload, timeout=60)
            except Exception as node_err:
                print(f"Warning: Node.js WhatsApp server unreachable - {node_err}")

        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/telegram-webhook")
async def telegram_webhook(update: dict):
    """Listens for Telegram 'Start' pings to bind the chat_id to the student."""
    try:
        message = update.get("message")
        if not message:
            return {"status": "ignored"}
        
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        
        # If the message is a deep link start command (e.g., "/start 1234-uuid-5678")
        if text.startswith("/start "):
            parts = text.split(" ")
            if len(parts) > 1:
                token = parts[1]
                
                # Find the student who owns this token
                res = supabase.table("students").select("id, first_name").eq("auth_link_token", token).execute()
                if res.data:
                    student_id = res.data[0]["id"]
                    first_name = res.data[0]["first_name"]
                    
                    # Lock in their Chat ID and burn the token
                    supabase.table("students").update({
                        "telegram_chat_id": str(chat_id),
                        "auth_link_token": None
                    }).eq("id", student_id).execute()
                    
                    # Fire a welcome message back to their Telegram app
                    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
                    if bot_token:
                        requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={
                            "chat_id": chat_id,
                            "text": f"✅ Success, {first_name}! Your Telegram is now securely linked to the RAM Portal."
                        })
                
        return {"status": "ok"}
    except Exception as e:
        print(f"Telegram Webhook Error: {str(e)}")
        # Return 200 OK so Telegram doesn't retry the webhook endlessly on a failure
        return {"status": "error"}