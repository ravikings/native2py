C=======================================================================
C     WELLIB   -   WELL MODEL, INFLOW PERFORMANCE AND NODAL ANALYSIS
C
C     WRITTEN   03-OCT-1989   D. LEBLANC
C     REVISED   11-JAN-1992   PEACEMAN ANISOTROPIC RE
C     REVISED   09-JUN-1994   VOGEL IPR BELOW BUBBLE POINT
C     REVISED   25-AUG-1996   NODAL SOLVE BRACKETS BEFORE SECANT
C
C     TIES TOGETHER SIMCOR (RESERVOIR SIDE), HYDRAU (TUBING SIDE) AND
C     PVTCOR (FLUID SIDE).  THIS IS THE ONLY PLACE THE THREE MEET.
C=======================================================================
      SUBROUTINE WELADD (NAME, ITYP, RW, SKIN, IW, JW, K1, K2, IWELL)
C-----------------------------------------------------------------------
C     ADD A VERTICAL WELL PERFORATED FROM LAYER K1 TO K2.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
C     --- native2py: expanded INCLUDE 'GRID.INC' ---
C=======================================================================
C     GRID.INC   -   RESERVOIR GRID AND SOLUTION ARRAYS
C
C     ORIGINALLY WRITTEN  08-JUL-1989   M. OKONKWO / R.T. HALSEY
C     REVISED             22-APR-1992   NX*NY*NZ RAISED TO 40000
C     REVISED             17-OCT-1995   ADDED NON-NEIGHBOUR CONNECTIONS
C
C     STORAGE IS COLUMN-MAJOR NATURAL ORDER:
C         L = I + (J-1)*NX + (K-1)*NX*NY
C     EVERY ROUTINE IN THIS LIBRARY ASSUMES THAT INDEXING.  IF YOU
C     CHANGE IT YOU WILL BREAK TRANX/TRANY/TRANZ AND THE JACOBIAN
C     BANDWIDTH ASSUMPTION IN MATSOL.
C=======================================================================
      PARAMETER (MXCELL = 40000)
      PARAMETER (MXWELL = 200)
      PARAMETER (MXPERF = 50)
      PARAMETER (MXNNC  = 2000)
C
C-----GRID DIMENSIONS ---------------------------------------------------
      COMMON /GDIM/ NX, NY, NZ, NCELL, NACTIV, NNNC
C
C-----STATIC ROCK PROPERTIES -------------------------------------------
      COMMON /ROCK/ PORO(MXCELL), PERMX(MXCELL), PERMY(MXCELL),
     &              PERMZ(MXCELL), DXC(MXCELL), DYC(MXCELL),
     &              DZC(MXCELL), TOPS(MXCELL), NTG(MXCELL)
C
C-----TRANSMISSIBILITIES (BUILT ONCE BY TRNCAL) ------------------------
      COMMON /TRANS/ TRANX(MXCELL), TRANY(MXCELL), TRANZ(MXCELL)
C
C-----PRIMARY UNKNOWNS AND OLD TIME LEVEL ------------------------------
      COMMON /STATE/ PRES(MXCELL), SWAT(MXCELL), SGAS(MXCELL),
     &               POLD(MXCELL), SWOLD(MXCELL), SGOLD(MXCELL)
C
C-----ACTIVE CELL MAP.  IACT(L) = 0 MEANS PINCHED OUT / INACTIVE --------
      COMMON /ACTMAP/ IACT(MXCELL), LNUM(MXCELL)
C
C-----WELL DATA --------------------------------------------------------
C     IWTYPE  1 = PRODUCER (BHP), 2 = PRODUCER (RATE),
C             3 = INJECTOR (BHP), 4 = INJECTOR (RATE)
C
      COMMON /WELLS/ WBHP(MXWELL), WRATE(MXWELL), WWI(MXWELL,MXPERF),
     &               WSKIN(MXWELL), WRAD(MXWELL),
     &               IWCELL(MXWELL,MXPERF), NPERF(MXWELL),
     &               IWTYPE(MXWELL), NWELL
      CHARACTER*8   WNAME
      COMMON /WNAMC/ WNAME(MXWELL)
C
C-----TIME STEPPING ----------------------------------------------------
      COMMON /TSTEP/ TIME, DT, DTMIN, DTMAX, DTFAC, TEND, NSTEP, NCUT
C
C     --- native2py: end INCLUDE 'GRID.INC' ---
      CHARACTER*8 NAME
C
      IF (NWELL .GE. MXWELL) THEN
         IERR = 61
         RETURN
      END IF
      IF (IW .LT. 1 .OR. IW .GT. NX .OR.
     &    JW .LT. 1 .OR. JW .GT. NY) THEN
         IERR = 62
         RETURN
      END IF
C
      NWELL  = NWELL + 1
      IWELL  = NWELL
      WNAME (IWELL) = NAME
      IWTYPE(IWELL) = ITYP
      WRAD  (IWELL) = RW
      WSKIN (IWELL) = SKIN
      WBHP  (IWELL) = 2000.0D0
      WRATE (IWELL) = ZERO
C
      NP = 0
      DO 100 K = K1, K2
         IF (K .LT. 1 .OR. K .GT. NZ) GO TO 100
         L = IW + (JW-1)*NX + (K-1)*NX*NY
         IF (IACT(L) .EQ. 0) GO TO 100
         IF (NP .GE. MXPERF) THEN
            IERR = 63
            GO TO 110
         END IF
         NP = NP + 1
         IWCELL(IWELL,NP) = L
         WWI   (IWELL,NP) = PEACEM(L, RW, SKIN)
  100 CONTINUE
  110 NPERF(IWELL) = NP
      IF (NP .EQ. 0) IERR = 64
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION PEACEM (L, RW, SKIN)
C-----------------------------------------------------------------------
C     PEACEMAN WELL INDEX FOR A VERTICAL WELL IN AN ANISOTROPIC BLOCK.
C     UNITS: RB-CP/D/PSI (MOBILITY APPLIED SEPARATELY BY WELSRC).
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
C     --- native2py: expanded INCLUDE 'GRID.INC' ---
C=======================================================================
C     GRID.INC   -   RESERVOIR GRID AND SOLUTION ARRAYS
C
C     ORIGINALLY WRITTEN  08-JUL-1989   M. OKONKWO / R.T. HALSEY
C     REVISED             22-APR-1992   NX*NY*NZ RAISED TO 40000
C     REVISED             17-OCT-1995   ADDED NON-NEIGHBOUR CONNECTIONS
C
C     STORAGE IS COLUMN-MAJOR NATURAL ORDER:
C         L = I + (J-1)*NX + (K-1)*NX*NY
C     EVERY ROUTINE IN THIS LIBRARY ASSUMES THAT INDEXING.  IF YOU
C     CHANGE IT YOU WILL BREAK TRANX/TRANY/TRANZ AND THE JACOBIAN
C     BANDWIDTH ASSUMPTION IN MATSOL.
C=======================================================================
      PARAMETER (MXCELL = 40000)
      PARAMETER (MXWELL = 200)
      PARAMETER (MXPERF = 50)
      PARAMETER (MXNNC  = 2000)
C
C-----GRID DIMENSIONS ---------------------------------------------------
      COMMON /GDIM/ NX, NY, NZ, NCELL, NACTIV, NNNC
C
C-----STATIC ROCK PROPERTIES -------------------------------------------
      COMMON /ROCK/ PORO(MXCELL), PERMX(MXCELL), PERMY(MXCELL),
     &              PERMZ(MXCELL), DXC(MXCELL), DYC(MXCELL),
     &              DZC(MXCELL), TOPS(MXCELL), NTG(MXCELL)
C
C-----TRANSMISSIBILITIES (BUILT ONCE BY TRNCAL) ------------------------
      COMMON /TRANS/ TRANX(MXCELL), TRANY(MXCELL), TRANZ(MXCELL)
C
C-----PRIMARY UNKNOWNS AND OLD TIME LEVEL ------------------------------
      COMMON /STATE/ PRES(MXCELL), SWAT(MXCELL), SGAS(MXCELL),
     &               POLD(MXCELL), SWOLD(MXCELL), SGOLD(MXCELL)
C
C-----ACTIVE CELL MAP.  IACT(L) = 0 MEANS PINCHED OUT / INACTIVE --------
      COMMON /ACTMAP/ IACT(MXCELL), LNUM(MXCELL)
C
C-----WELL DATA --------------------------------------------------------
C     IWTYPE  1 = PRODUCER (BHP), 2 = PRODUCER (RATE),
C             3 = INJECTOR (BHP), 4 = INJECTOR (RATE)
C
      COMMON /WELLS/ WBHP(MXWELL), WRATE(MXWELL), WWI(MXWELL,MXPERF),
     &               WSKIN(MXWELL), WRAD(MXWELL),
     &               IWCELL(MXWELL,MXPERF), NPERF(MXWELL),
     &               IWTYPE(MXWELL), NWELL
      CHARACTER*8   WNAME
      COMMON /WNAMC/ WNAME(MXWELL)
C
C-----TIME STEPPING ----------------------------------------------------
      COMMON /TSTEP/ TIME, DT, DTMIN, DTMAX, DTFAC, TEND, NSTEP, NCUT
C
C     --- native2py: end INCLUDE 'GRID.INC' ---
      DATA CDARCY /0.001127D0/
      DATA TWOPI /6.28318530717959D0/
C
      PX = MAX(PERMX(L), SMALL)
      PY = MAX(PERMY(L), SMALL)
      RQ = DSQRT(PY/PX)
      RE = 0.28D0 * DSQRT(RQ*DXC(L)**2 + DSQRT(PX/PY)*DYC(L)**2)
     &     / (DSQRT(RQ) + DSQRT(DSQRT(PX/PY)))
      IF (RE .LE. RW .OR. RW .LE. SMALL) THEN
         IERR   = 65
         PEACEM = ZERO
         RETURN
      END IF
      PEACEM = TWOPI * CDARCY * DSQRT(PX*PY) * DZC(L) * NTG(L)
     &         / (DLOG(RE/RW) + SKIN)
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE WELSRC (AD, RHS)
C-----------------------------------------------------------------------
C     ADD WELL SOURCE TERMS INTO THE IMPES PRESSURE MATRIX.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
C     --- native2py: expanded INCLUDE 'GRID.INC' ---
C=======================================================================
C     GRID.INC   -   RESERVOIR GRID AND SOLUTION ARRAYS
C
C     ORIGINALLY WRITTEN  08-JUL-1989   M. OKONKWO / R.T. HALSEY
C     REVISED             22-APR-1992   NX*NY*NZ RAISED TO 40000
C     REVISED             17-OCT-1995   ADDED NON-NEIGHBOUR CONNECTIONS
C
C     STORAGE IS COLUMN-MAJOR NATURAL ORDER:
C         L = I + (J-1)*NX + (K-1)*NX*NY
C     EVERY ROUTINE IN THIS LIBRARY ASSUMES THAT INDEXING.  IF YOU
C     CHANGE IT YOU WILL BREAK TRANX/TRANY/TRANZ AND THE JACOBIAN
C     BANDWIDTH ASSUMPTION IN MATSOL.
C=======================================================================
      PARAMETER (MXCELL = 40000)
      PARAMETER (MXWELL = 200)
      PARAMETER (MXPERF = 50)
      PARAMETER (MXNNC  = 2000)
C
C-----GRID DIMENSIONS ---------------------------------------------------
      COMMON /GDIM/ NX, NY, NZ, NCELL, NACTIV, NNNC
C
C-----STATIC ROCK PROPERTIES -------------------------------------------
      COMMON /ROCK/ PORO(MXCELL), PERMX(MXCELL), PERMY(MXCELL),
     &              PERMZ(MXCELL), DXC(MXCELL), DYC(MXCELL),
     &              DZC(MXCELL), TOPS(MXCELL), NTG(MXCELL)
C
C-----TRANSMISSIBILITIES (BUILT ONCE BY TRNCAL) ------------------------
      COMMON /TRANS/ TRANX(MXCELL), TRANY(MXCELL), TRANZ(MXCELL)
C
C-----PRIMARY UNKNOWNS AND OLD TIME LEVEL ------------------------------
      COMMON /STATE/ PRES(MXCELL), SWAT(MXCELL), SGAS(MXCELL),
     &               POLD(MXCELL), SWOLD(MXCELL), SGOLD(MXCELL)
C
C-----ACTIVE CELL MAP.  IACT(L) = 0 MEANS PINCHED OUT / INACTIVE --------
      COMMON /ACTMAP/ IACT(MXCELL), LNUM(MXCELL)
C
C-----WELL DATA --------------------------------------------------------
C     IWTYPE  1 = PRODUCER (BHP), 2 = PRODUCER (RATE),
C             3 = INJECTOR (BHP), 4 = INJECTOR (RATE)
C
      COMMON /WELLS/ WBHP(MXWELL), WRATE(MXWELL), WWI(MXWELL,MXPERF),
     &               WSKIN(MXWELL), WRAD(MXWELL),
     &               IWCELL(MXWELL,MXPERF), NPERF(MXWELL),
     &               IWTYPE(MXWELL), NWELL
      CHARACTER*8   WNAME
      COMMON /WNAMC/ WNAME(MXWELL)
C
C-----TIME STEPPING ----------------------------------------------------
      COMMON /TSTEP/ TIME, DT, DTMIN, DTMAX, DTFAC, TEND, NSTEP, NCUT
C
C     --- native2py: end INCLUDE 'GRID.INC' ---
      DIMENSION AD(MXCELL), RHS(MXCELL)
      DOUBLE PRECISION KROIL, KRWAT, KRGAS
C
      DO 200 IW = 1, NWELL
         DO 100 IP = 1, NPERF(IW)
            L = IWCELL(IW,IP)
            IF (IACT(L) .EQ. 0) GO TO 100
            CALL PVTSET (PRES(L))
            IF (IERR .NE. 0) RETURN
C
            IF (IWTYPE(IW) .EQ. 3 .OR. IWTYPE(IW) .EQ. 4) THEN
C              ---- WATER INJECTOR, TOTAL MOBILITY OF WATER ONLY
               XM = ONE / MAX(VISW*BW, SMALL)
            ELSE
               XM = KROIL(SWAT(L), SGAS(L)) / MAX(VISO*BO, SMALL)
     &            + KRWAT(SWAT(L))          / MAX(VISW*BW, SMALL)
     &            + KRGAS(SGAS(L))          / MAX(VISG*BG, SMALL)
            END IF
            WI = WWI(IW,IP) * XM
C
            IF (IWTYPE(IW) .EQ. 1 .OR. IWTYPE(IW) .EQ. 3) THEN
C              ---- BHP CONTROL
               AD (L) = AD (L) + WI
               RHS(L) = RHS(L) + WI*WBHP(IW)
            ELSE
C              ---- RATE CONTROL, DISTRIBUTED BY WELL INDEX
               RHS(L) = RHS(L) - WRATE(IW) / DBLE(MAX(NPERF(IW),1))
            END IF
  100    CONTINUE
  200 CONTINUE
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION IPRVOG (PRAVG, PWF, QMAX)
C-----------------------------------------------------------------------
C     VOGEL INFLOW PERFORMANCE, STB/D.  COMPOSITE ABOVE / BELOW PB.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
C
      IF (PRAVG .LE. SMALL) THEN
         IPRVOG = ZERO
         RETURN
      END IF
      PW = MAX(PWF, ZERO)
      IF (PW .GE. PRAVG) THEN
         IPRVOG = ZERO
         RETURN
      END IF
C
      IF (PRAVG .LE. PB) THEN
         X = PW / PRAVG
         IPRVOG = QMAX * (ONE - 0.2D0*X - 0.8D0*X*X)
      ELSE IF (PW .GE. PB) THEN
C        ---- STRAIGHT LINE PI SEGMENT ABOVE THE BUBBLE POINT
         DEN = (PRAVG - PB) + PB/1.8D0
         IF (DEN .LT. SMALL) THEN
            IPRVOG = ZERO
         ELSE
            IPRVOG = QMAX * (PRAVG - PW) / DEN
         END IF
      ELSE
         DEN = (PRAVG - PB) + PB/1.8D0
         IF (DEN .LT. SMALL) THEN
            IPRVOG = ZERO
            RETURN
         END IF
         QB = QMAX * (PRAVG - PB) / DEN
         X  = PW / PB
         IPRVOG = QB + (QMAX - QB)/1.8D0
     &            * 1.8D0 * (ONE - 0.2D0*X - 0.8D0*X*X)
      END IF
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE NODAL (PRAVG, QMAX, PWH, DIA, EPS, TVD, MD, WCUT,
     &                  GOR, QSOL, PWFSOL, ICONV)
C-----------------------------------------------------------------------
C     NODAL ANALYSIS AT THE BOTTOMHOLE NODE.  FIND THE RATE AT WHICH
C     THE IPR AND THE TUBING PERFORMANCE CURVE INTERSECT.
C
C         INFLOW   PWF = IPR INVERSE OF Q
C         OUTFLOW  PWF = TRAVER(PWH) WITH Q SPLIT BY WCUT AND GOR
C
C     SOLVED BY BRACKETING ON Q THEN SECANT.  THE 1989 VERSION USED
C     A FIXED 50 POINT SCAN AND PICKED THE CLOSEST POINT, WHICH IS
C     WHY OLD .PRT FILES SHOW RATES QUANTISED TO QMAX/50.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DOUBLE PRECISION MD, IPRVOG
      DATA TOLQ /1.0D-3/, ITMAX /50/
C
      ICONV = 1
      IF (QMAX .LE. SMALL) THEN
         QSOL   = ZERO
         PWFSOL = PRAVG
         RETURN
      END IF
C
      QLO = 1.0D-3*QMAX
      QHI = 0.999D0*QMAX
      FLO = GAPFUN(QLO, PRAVG, QMAX, PWH, DIA, EPS, TVD, MD, WCUT, GOR)
      IF (IERR .NE. 0) RETURN
      FHI = GAPFUN(QHI, PRAVG, QMAX, PWH, DIA, EPS, TVD, MD, WCUT, GOR)
      IF (IERR .NE. 0) RETURN
C
      IF (FLO*FHI .GT. ZERO) THEN
C        ---- NO INTERSECTION.  WELL WILL NOT FLOW ON THIS TUBING.
         QSOL   = ZERO
         PWFSOL = PRAVG
         ICONV  = 2
         RETURN
      END IF
C
      Q1 = QLO
      Q2 = QHI
      F1 = FLO
      F2 = FHI
      DO 100 IT = 1, ITMAX
         IF (DABS(F2 - F1) .LT. SMALL) GO TO 200
         Q3 = Q2 - F2*(Q2 - Q1)/(F2 - F1)
         IF (Q3 .LT. QLO) Q3 = QLO
         IF (Q3 .GT. QHI) Q3 = QHI
         F3 = GAPFUN(Q3, PRAVG, QMAX, PWH, DIA, EPS, TVD, MD,
     &               WCUT, GOR)
         IF (IERR .NE. 0) RETURN
         Q1 = Q2
         F1 = F2
         Q2 = Q3
         F2 = F3
         IF (DABS(Q2 - Q1) .LT. TOLQ*QMAX) GO TO 200
  100 CONTINUE
      ICONV = 3
C
  200 QSOL   = Q2
      PWFSOL = PRAVG - (PRAVG - PWFIPR(Q2, PRAVG, QMAX))
      PWFSOL = PWFIPR(Q2, PRAVG, QMAX)
      IF (ICONV .EQ. 1) ICONV = 0
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION PWFIPR (Q, PRAVG, QMAX)
C-----------------------------------------------------------------------
C     INVERT THE VOGEL IPR FOR PWF GIVEN Q.  BISECTION - THE QUADRATIC
C     INVERSE IS ONLY VALID FOR THE FULLY SATURATED BRANCH.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DOUBLE PRECISION IPRVOG
C
      PLO = ZERO
      PHI = PRAVG
      DO 100 IT = 1, 60
         PM = HALF*(PLO + PHI)
         IF (IPRVOG(PRAVG, PM, QMAX) .GT. Q) THEN
            PLO = PM
         ELSE
            PHI = PM
         END IF
         IF (PHI - PLO .LT. 1.0D-6*MAX(ONE,PRAVG)) GO TO 110
  100 CONTINUE
  110 PWFIPR = HALF*(PLO + PHI)
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION GAPFUN (Q, PRAVG, QMAX, PWH, DIA, EPS,
     &                                  TVD, MD, WCUT, GOR)
C-----------------------------------------------------------------------
C     INFLOW PWF MINUS OUTFLOW PWF AT LIQUID RATE Q.  ROOT = SOLUTION.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DOUBLE PRECISION MD
C
      QO = Q * (ONE - WCUT)
      QW = Q * WCUT
      QG = QO * GOR / 1000.0D0
      NSEG = 25
      CALL TRAVER (PWH, QO, QW, QG, DIA, EPS, TVD, MD, NSEG, PBH)
      IF (IERR .NE. 0) THEN
         GAPFUN = ZERO
         RETURN
      END IF
      GAPFUN = PWFIPR(Q, PRAVG, QMAX) - PBH
      RETURN
      END