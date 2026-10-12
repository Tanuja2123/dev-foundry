import type { ProjectOverview } from '../types'

function apiBaseUrl() {
  const configured = import.meta.env.VITE_API_BASE_URL?.trim()
  return (configured || 'http://localhost:8000').replace(/\/$/, '')
}

/**
 * ============================================================================
 * BACKEND INTEGRATION PLACEHOLDER:
 * Endpoint Expected: GET /api/projects
 * Response Expected: ProjectOverview[]
 *
 * NOTE FOR BACKEND DEVELOPER:
 * Please implement GET /api/projects returning a JSON array matching the
 * `ProjectOverview` type in `frontend/src/types.ts`.
 * In the meantime, the frontend will automatically use the mock data below
 * if the backend endpoint is not yet available or returns a 404/500 error.
 * ============================================================================
 */
export const MOCK_PROJECTS: ProjectOverview[] = [
  {
    conversation_id: 'b97e7c24-ee60-480c-8bd5-0e9f5840b8f7',
    project_name: 'Clinic Appointment Booking System',
    description:
      'A responsive web application for small clinics enabling patients to book, reschedule, cancel, and rate appointments online, with email reminders and staff schedule management.',
    created_at: '2026-10-08T10:47:19.508333+00:00',
    prd: {
      status: 'created',
      updated_at: '2026-10-08T11:15:00.000000+00:00',
      content: `## **Objective**

Develop a responsive web application for a small clinic that enables patients to book, reschedule, and cancel appointments online, rate their visit after the appointment, receive email reminders, and allows staff to efficiently manage doctors' schedules and appointments through an admin dashboard.

---

## **Problem Statement**

The clinic's current appointment management process is manual and inefficient, leading to scheduling errors, missed appointments, lack of patient feedback collection, and a poor patient experience. There is no centralized digital platform for patients to self-serve, provide feedback, or for staff to efficiently coordinate doctor availability and appointment bookings.

---

## **Goals**

- Enable patients to easily book, reschedule, and cancel appointments online.
- Send timely email reminders to patients 24 hours before scheduled appointments.
- Allow patients to securely view and manage their appointment history.
- Allow patients to rate their visit after each completed appointment.
- Provide staff with an admin dashboard for managing doctors' schedules, appointments, staff accounts, and doctor profiles.
- Support secure staff authentication with email/password and role-based permissions.
- Ensure a clean, responsive user experience for both patients (on mobile and desktop browsers) and staff (on desktop browsers).

---

## **User Experience**

Patients access the web app via mobile or desktop browser. Registration requires email and phone number, with email verification for account activation. Once logged in, patients can view doctor availability, select appointment type (General Consultation or Follow-up), choose time slots, and manage bookings. After attending an appointment, patients are prompted to rate their visit (e.g., 1–5 stars and optional comment) via a notification in the app and/or a follow-up email. Automated email reminders are sent 24 hours before each appointment. Patients can reschedule or cancel appointments up to 4 hours before the scheduled time; after this, cancellations require a phone call to the clinic. Staff log in using email and password via desktop browser. Receptionists manage appointments and doctor schedules; Admins manage staff accounts and doctor profiles in addition to receptionist functions. The interface features clean, generic branding suitable for future customization.

---

## **User Flow**

1. Patient accesses the web app and registers with email and phone number.
2. Patient verifies email to activate the account.
3. Patient logs in and views available time slots for two appointment types: General Consultation (30 min) and Follow-up (15 min).
4. Patient selects a doctor, chooses appointment type and preferred time slot, and books the appointment.
5. Patient receives confirmation of booking and can view, reschedule, or cancel appointments from their dashboard.
6. Patients can reschedule or cancel appointments up to 4 hours before their scheduled time; after that, cancellation is only possible by calling the clinic.
7. Patient receives an automated email reminder 24 hours before their appointment.
8. After their appointment is marked as completed, patient receives an in-app prompt and/or email invitation to rate their visit (star rating and optional feedback).
9. Patient submits their visit rating and feedback.
10. Staff logs in as Receptionist or Admin on the admin dashboard.
11. Receptionists can view and manage all appointments and update doctors' schedules.
12. Admins can perform all receptionist functions and also manage staff accounts and doctor profiles.`,
    },
    trd: {
      status: 'created',
      updated_at: '2026-10-08T11:30:00.000000+00:00',
      content: `## Technical Requirements Document (TRD)

Below is a comprehensive overview of the proposed technology stack for the Clinic Appointment Booking System.

---

### **Frontend Technology Stack**

- **Framework:** React
- **Supporting Technologies:**
  - Redux Toolkit (state management)
  - React Router (client-side routing)
  - Styled Components (styling)
  - Ant Design (UI component system)
- **Tools & Libraries:**
  - Vite (build tool & development server)
  - Jest & React Testing Library (unit/component testing)
- **Rationale:**
  React is well-suited for building responsive, interactive web interfaces for both patients and staff, with strong mobile and desktop browser support. Redux Toolkit streamlines state management for booking flows and dashboard data. Ant Design provides robust, customizable UI components for speedy development of professional admin and patient interfaces.

---

### **Backend Technology Stack**

- **Language:** Node.js (TypeScript)
- **Framework:** Express.js
- **API Technologies:**
  - REST (primary API)
  - WebSocket (prepped for live schedule/admin dashboard sync)
- **Supporting Technologies:**
  - Nodemailer (email delivery)
  - BullMQ (background jobs and scheduling email reminders)
  - Passport.js (authentication and role-based access control)
  - bcrypt (secure password hashing)
- **Rationale:**
  Node.js with Express.js and TypeScript offers fast API development, maintainable code, and strong ecosystem support. REST APIs ensure compatibility for both web and future mobile/desktop extensions. BullMQ enables scheduling tasks like email reminders. Passport.js with role-based flow enforces secure access for staff/patient roles.

---

### **Database Technology Stack**

- **Database:** PostgreSQL
- **Database Type:** Relational
- **Supporting Technologies:**
  - Prisma (ORM)
  - dbmate (database migrations)
  - Redis (caching and BullMQ job queue backend)
- **Rationale:**
  PostgreSQL is robust for handling relational data such as users, appointments, ratings, and schedules, suitable for small-to-medium scale. Prisma ORM accelerates database interactions with type safety, while Redis serves both as a cache and as the backend for the job/work queue infrastructure.`,
    },
    project_link:
      'https://dev.azure.com/myorg/Clinic%20Appointment%20Manager',
    repo_link:
      'https://dev.azure.com/myorg/Clinic%20Appointment%20Manager/_git/clinic-appointment-manager',
  },
  {
    conversation_id: 'e41d89b3-1122-43fa-a678-0f98e721ab91',
    project_name: 'E-Commerce Inventory Tracker',
    description:
      'High-throughput warehouse inventory service with barcode scanner integration, low-stock threshold triggers, and real-time vendor restock syncing.',
    created_at: '2026-10-09T08:14:22.100000+00:00',
    prd: {
      status: 'created',
      updated_at: '2026-10-09T08:45:00.000000+00:00',
      content: `## **Objective**

Build a centralized inventory management portal for multi-warehouse retail logistics.

---

## **Problem Statement**

Current warehouse logs are maintained across disparate spreadsheets, causing stockouts, delayed fulfillment, and inaccurate availability metrics for the customer-facing storefront.

---

## **Goals**

- Real-time inventory sync across 4 regional warehouses.
- Automated low-stock alerts sent to supplier procurement teams.
- Barcode scanning integration via mobile browser camera.`,
    },
    trd: {
      status: 'not_created',
      content: null,
      updated_at: null,
    },
    project_link: null,
    repo_link: null,
  },
  {
    conversation_id: 'fa82910c-9821-4f11-b012-349021def981',
    project_name: 'Smart IoT Telemetry Gateway',
    description:
      'Edge gateway data ingestion pipeline collecting temperature, humidity, and vibration sensor telemetry over MQTT.',
    created_at: '2026-10-09T07:30:11.450000+00:00',
    prd: {
      status: 'not_created',
      content: null,
      updated_at: null,
    },
    trd: {
      status: 'not_created',
      content: null,
      updated_at: null,
    },
    project_link: null,
    repo_link: null,
  },
  {
    conversation_id: 'c90a1492-3810-47de-8891-910283fa7129',
    project_name: null,
    description: null,
    created_at: '2026-10-09T09:15:00.000000+00:00',
    prd: {
      status: 'not_created',
      content: null,
      updated_at: null,
    },
    trd: {
      status: 'not_created',
      content: null,
      updated_at: null,
    },
    project_link: null,
    repo_link: null,
  },
]

/**
 * Fetches the projects list from the backend API.
 * Falls back gracefully to `MOCK_PROJECTS` if the backend endpoint is not yet ready.
 */
export async function fetchProjectsOverview(): Promise<ProjectOverview[]> {
  const url = `${apiBaseUrl()}/api/projects`
  try {
    const response = await fetch(url)
    if (response.ok) {
      const data = (await response.json()) as ProjectOverview[]
      if (Array.isArray(data)) {
        return data
      }
    }
  } catch {
    // Backend endpoint not yet implemented or unreachable. Fall back to mock.
  }

  // Graceful fallback to mock data
  return MOCK_PROJECTS
}
