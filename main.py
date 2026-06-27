import os
from typing import List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

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

# Define the expected incoming data structure
class StaffRequest(BaseModel):
    fullName: str
    email: str
    password: str
    department: str
    role_level: str

# --- ADDED FOR LECTURERS & HOD ---
class GradeEntry(BaseModel):
    student_name: str
    admission_number: str
    cat_score: float
    exam_score: float

class GradeSubmission(BaseModel):
    block_name: str
    unit_name: str
    lecturer_id: str
    grades: List[GradeEntry]

class ApprovalRequest(BaseModel):
    block_name: str
    unit_name: str
# ---------------------------------

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

        # 4. Return success
        return {"success": True, "userId": user_id}

    except Exception as e:
        # If anything fails, catch the error and send a clean 400 Bad Request back
        print(f"Error creating staff: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

# ==========================================
# LECTURER FUNCTION: SUBMIT GRADES
# ==========================================
@app.post("/api/submit-grades")
async def submit_grades(request: GradeSubmission):
    try:
        payload = []
        for entry in request.grades:
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
        return {"success": True, "message": f"Successfully submitted {len(payload)} grades to HOD."}

    except Exception as e:
        print(f"Server Error: {str(e)}")
        raise HTTPException(status_code=400, detail="Failed to process grades securely on the server.")

# ==========================================
# HOD FUNCTION: APPROVE RESULTS
# ==========================================
class ApprovalRequest(BaseModel):
    block_name: str
    unit_name: str
    action: str # "Approve" or "Reject"

# ==========================================
# HOD FUNCTION: APPROVE OR REJECT RESULTS
# ==========================================
@app.post("/api/approve-results")
async def approve_results(request: ApprovalRequest):
    try:
        # Determine the final status
        final_status = "Approved" if request.action == "Approve" else "Rejected"
        
        # Update the database securely
        supabase.table("exam_results").update({"status": final_status})\
            .eq("block_name", request.block_name)\
            .eq("unit_name", request.unit_name)\
            .eq("status", "Pending").execute()
            
        return {"success": True, "status": final_status}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))