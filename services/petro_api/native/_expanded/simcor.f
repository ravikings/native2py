C=======================================================================
C     SIMCOR   -   IMPES BLACK OIL SIMULATOR CORE
C
C     WRITTEN   08-JUL-1989   M. OKONKWO / R.T. HALSEY
C     REVISED   07-MAY-1991   LSOR PRESSURE SOLVE (SEE MATSOL)
C     REVISED   22-APR-1992   MXCELL RAISED TO 40000
C     REVISED   17-OCT-1995   NON NEIGHBOUR CONNECTIONS
C     REVISED   30-JAN-1997   TIME STORED AS DAYS SINCE START, NOT AS
C                             A TWO DIGIT YEAR PLUS DAY OF YEAR
C
C     CALL SEQUENCE EXPECTED BY THE C++ DRIVER (Simulator.cpp):
C         CALL SIMINI (NX, NY, NZ)
C         CALL GRDSET (...)          ONCE PER PROPERTY ARRAY
C         CALL TRNCAL
C         CALL EQUILI (WOC, GOC, PDAT, DDAT)
C         CALL STEP   (DTIN, DTOUT, ICONV)    REPEATEDLY
C
C     SIMINI MUST BE CALLED AFTER PVTINI AND KRINI.  THERE IS NO CHECK.
C=======================================================================
      SUBROUTINE SIMINI (MX, MY, MZ)
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
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
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
C     --- nativegate: expanded INCLUDE 'GRID.INC' ---
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
C     --- nativegate: end INCLUDE 'GRID.INC' ---
C
      IF (MX*MY*MZ .GT. MXCELL) THEN
         IERR = 51
         IF (LUNPRT .GT. 0) WRITE (LUNPRT,9500) MX*MY*MZ, MXCELL
         RETURN
      END IF
      NX    = MX
      NY    = MY
      NZ    = MZ
      NCELL = MX*MY*MZ
      NNNC  = 0
      NWELL = 0
C
      DO 100 L = 1, NCELL
         IACT (L) = 1
         LNUM (L) = L
         PORO (L) = 0.20D0
         PERMX(L) = 100.0D0
         PERMY(L) = 100.0D0
         PERMZ(L) = 10.0D0
         NTG  (L) = ONE
         DXC  (L) = 100.0D0
         DYC  (L) = 100.0D0
         DZC  (L) = 20.0D0
         TOPS (L) = 8000.0D0
         PRES (L) = 4000.0D0
         SWAT (L) = 0.25D0
         SGAS (L) = ZERO
  100 CONTINUE
      NACTIV = NCELL
C
      TIME  = ZERO
      DT    = 1.0D0
      DTMIN = 1.0D-3
      DTMAX = 30.0D0
      DTFAC = 1.5D0
      NSTEP = 0
      NCUT  = 0
      RETURN
 9500 FORMAT (' *** SIMINI - GRID OF',I8,' CELLS EXCEEDS MXCELL =',I8)
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE GRDSET (IPROP, VALS, N)
C-----------------------------------------------------------------------
C     BULK LOAD ONE PROPERTY ARRAY FROM THE DECK READER.
C     IPROP  1 PORO  2 PERMX  3 PERMY  4 PERMZ  5 DZ  6 TOPS  7 NTG
C            8 ACTNUM
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
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
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
C     --- nativegate: expanded INCLUDE 'GRID.INC' ---
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
C     --- nativegate: end INCLUDE 'GRID.INC' ---
      DIMENSION VALS(N)
C
      IF (N .NE. NCELL) THEN
         IERR = 52
         RETURN
      END IF
C
      GO TO (10, 20, 30, 40, 50, 60, 70, 80), IPROP
      IERR = 53
      RETURN
C
   10 DO 11 L = 1, N
   11 PORO(L)  = VALS(L)
      RETURN
   20 DO 21 L = 1, N
   21 PERMX(L) = VALS(L)
      RETURN
   30 DO 31 L = 1, N
   31 PERMY(L) = VALS(L)
      RETURN
   40 DO 41 L = 1, N
   41 PERMZ(L) = VALS(L)
      RETURN
   50 DO 51 L = 1, N
   51 DZC(L)   = VALS(L)
      RETURN
   60 DO 61 L = 1, N
   61 TOPS(L)  = VALS(L)
      RETURN
   70 DO 71 L = 1, N
   71 NTG(L)   = VALS(L)
      RETURN
   80 CONTINUE
      NACTIV = 0
      DO 81 L = 1, N
         IACT(L) = INT(VALS(L))
         IF (IACT(L) .NE. 0) NACTIV = NACTIV + 1
   81 CONTINUE
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE TRNCAL
C-----------------------------------------------------------------------
C     HARMONIC AVERAGE INTERBLOCK TRANSMISSIBILITIES.  DARCY CONSTANT
C     0.001127 GIVES RB/D/PSI FROM MD, FT AND CP.
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
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
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
C     --- nativegate: expanded INCLUDE 'GRID.INC' ---
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
C     --- nativegate: end INCLUDE 'GRID.INC' ---
      DATA CDARCY /0.001127D0/
C
      DO 300 K = 1, NZ
      DO 200 J = 1, NY
      DO 100 I = 1, NX
         L = I + (J-1)*NX + (K-1)*NX*NY
         TRANX(L) = ZERO
         TRANY(L) = ZERO
         TRANZ(L) = ZERO
         IF (IACT(L) .EQ. 0) GO TO 100
C
         IF (I .LT. NX) THEN
            M = L + 1
            IF (IACT(M) .NE. 0) THEN
               A1 = DYC(L)*DZC(L)*NTG(L)
               A2 = DYC(M)*DZC(M)*NTG(M)
               R1 = DXC(L) / MAX(PERMX(L)*A1, SMALL)
               R2 = DXC(M) / MAX(PERMX(M)*A2, SMALL)
               TRANX(L) = 2.0D0*CDARCY / (R1 + R2)
            END IF
         END IF
C
         IF (J .LT. NY) THEN
            M = L + NX
            IF (IACT(M) .NE. 0) THEN
               A1 = DXC(L)*DZC(L)*NTG(L)
               A2 = DXC(M)*DZC(M)*NTG(M)
               R1 = DYC(L) / MAX(PERMY(L)*A1, SMALL)
               R2 = DYC(M) / MAX(PERMY(M)*A2, SMALL)
               TRANY(L) = 2.0D0*CDARCY / (R1 + R2)
            END IF
         END IF
C
         IF (K .LT. NZ) THEN
            M = L + NX*NY
            IF (IACT(M) .NE. 0) THEN
               A1 = DXC(L)*DYC(L)
               A2 = DXC(M)*DYC(M)
               R1 = DZC(L) / MAX(PERMZ(L)*A1, SMALL)
               R2 = DZC(M) / MAX(PERMZ(M)*A2, SMALL)
               TRANZ(L) = 2.0D0*CDARCY / (R1 + R2)
            END IF
         END IF
  100 CONTINUE
  200 CONTINUE
  300 CONTINUE
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE EQUILI (WOC, GOC, PDAT, DDAT)
C-----------------------------------------------------------------------
C     GRAVITY CAPILLARY EQUILIBRIUM INITIALISATION.
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
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
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
C     --- nativegate: expanded INCLUDE 'GRID.INC' ---
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
C     --- nativegate: end INCLUDE 'GRID.INC' ---
      COMMON /KREND/ SWCON, SORW, SORG, SGCON, EXPW, EXPO, EXPG,
     &               KRWMAX, KROMAX, KRGMAX, PCWMAX, PCGMAX
      DOUBLE PRECISION KRWMAX, KROMAX, KRGMAX
C
      CALL PVTSET (PDAT)
      IF (IERR .NE. 0) RETURN
      GRADO = RHOO / 144.0D0
      GRADW = RHOW / 144.0D0
C
      DO 300 K = 1, NZ
      DO 200 J = 1, NY
      DO 100 I = 1, NX
         L = I + (J-1)*NX + (K-1)*NX*NY
         IF (IACT(L) .EQ. 0) GO TO 100
         DEPTH   = TOPS(L) + HALF*DZC(L)
         PRES(L) = PDAT + GRADO*(DEPTH - DDAT)
         IF (DEPTH .GE. WOC) THEN
            SWAT(L) = ONE - SORW
            SGAS(L) = ZERO
         ELSE IF (DEPTH .LE. GOC) THEN
            SWAT(L) = SWCON
            SGAS(L) = ONE - SWCON - SORG
         ELSE
            SWAT(L) = SWCON
            SGAS(L) = ZERO
         END IF
         POLD (L) = PRES(L)
         SWOLD(L) = SWAT(L)
         SGOLD(L) = SGAS(L)
  100 CONTINUE
  200 CONTINUE
  300 CONTINUE
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE STEP (DTIN, DTOUT, ICONV)
C-----------------------------------------------------------------------
C     ADVANCE ONE IMPES TIME STEP.  ON FAILURE THE STEP IS HALVED AND
C     RETRIED UP TO 8 TIMES BEFORE ICONV IS RETURNED NON ZERO.
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
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
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
C     --- nativegate: expanded INCLUDE 'GRID.INC' ---
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
C     --- nativegate: end INCLUDE 'GRID.INC' ---
      COMMON /MATRIX/ AE(MXCELL), AW(MXCELL), AN(MXCELL), AS(MXCELL),
     &                AT(MXCELL), AB(MXCELL), AD(MXCELL), RHS(MXCELL),
     &                DP(MXCELL)
C
      DT    = MAX(DTMIN, MIN(DTIN, DTMAX))
      ICONV = 0
C
      DO 100 L = 1, NCELL
         POLD (L) = PRES(L)
         SWOLD(L) = SWAT(L)
         SGOLD(L) = SGAS(L)
  100 CONTINUE
C
      DO 500 ITRY = 1, 8
         CALL JACOBI (AE, AW, AN, AS, AT, AB, AD, RHS)
         IF (IERR .NE. 0) GO TO 400
C
         DO 200 L = 1, NCELL
            DP(L) = ZERO
  200    CONTINUE
C
         CALL LSOR (AE, AW, AN, AS, AT, AB, AD, RHS, DP,
     &              1.30D0, 1.0D-6, 200, ITUSED, RESID)
         IF (IERR .NE. 0) GO TO 400
         IF (RESID .GT. 1.0D-4) GO TO 400
C
         DO 210 L = 1, NCELL
            IF (IACT(L) .NE. 0) PRES(L) = POLD(L) + DP(L)
  210    CONTINUE
C
         CALL SATUPD (ISAT)
         IF (ISAT .NE. 0) GO TO 400
C
         TIME  = TIME + DT
         NSTEP = NSTEP + 1
         DTOUT = MIN(DT*DTFAC, DTMAX)
         RETURN
C
  400    CONTINUE
C        ---- CUT AND RETRY
         IERR = 0
         NCUT = NCUT + 1
         DT   = HALF*DT
         DO 410 L = 1, NCELL
            PRES(L) = POLD (L)
            SWAT(L) = SWOLD(L)
            SGAS(L) = SGOLD(L)
  410    CONTINUE
         IF (DT .LT. DTMIN) GO TO 900
  500 CONTINUE
C
  900 ICONV = 1
      DTOUT = DTMIN
      IF (LUNPRT .GT. 0) WRITE (LUNPRT,9600) TIME, NCUT
      RETURN
 9600 FORMAT (' *** STEP FAILED AT TIME =',F12.3,' DAYS AFTER',I4,
     &        ' CUTS')
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE JACOBI (AE, AW, AN, AS, AT, AB, AD, RHS)
C-----------------------------------------------------------------------
C     ASSEMBLE THE IMPES PRESSURE EQUATION.  MOBILITIES ARE UPSTREAM
C     WEIGHTED ON THE OLD TIME LEVEL POTENTIAL.
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
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
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
C     --- nativegate: expanded INCLUDE 'GRID.INC' ---
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
C     --- nativegate: end INCLUDE 'GRID.INC' ---
      DIMENSION AE(MXCELL), AW(MXCELL), AN(MXCELL), AS(MXCELL)
      DIMENSION AT(MXCELL), AB(MXCELL), AD(MXCELL), RHS(MXCELL)
      DOUBLE PRECISION KROIL, KRWAT, KRGAS
C
      DO 100 L = 1, NCELL
         AE(L) = ZERO
         AW(L) = ZERO
         AN(L) = ZERO
         AS(L) = ZERO
         AT(L) = ZERO
         AB(L) = ZERO
         AD(L) = ZERO
         RHS(L)= ZERO
  100 CONTINUE
C
      DO 400 K = 1, NZ
      DO 300 J = 1, NY
      DO 200 I = 1, NX
         L = I + (J-1)*NX + (K-1)*NX*NY
         IF (IACT(L) .EQ. 0) THEN
            AD(L) = ONE
            GO TO 200
         END IF
C
         CALL PVTSET (PRES(L))
         IF (IERR .NE. 0) RETURN
         SO   = ONE - SWAT(L) - SGAS(L)
         XMOB = KROIL(SWAT(L), SGAS(L)) / MAX(VISO*BO, SMALL)
     &        + KRWAT(SWAT(L))          / MAX(VISW*BW, SMALL)
     &        + KRGAS(SGAS(L))          / MAX(VISG*BG, SMALL)
C
         VP = DXC(L)*DYC(L)*DZC(L)*PORO(L)*NTG(L) / 5.615D0
         CT = CO*SO + CW*SWAT(L) + CG*SGAS(L) + 4.0D-6
C        ---- ACCUMULATE.  A PLAIN ASSIGNMENT HERE WOULD DISCARD THE
C        ---- NEIGHBOUR CONTRIBUTIONS ALREADY POSTED INTO AD(L) BY THE
C        ---- CELLS TO THE WEST, SOUTH AND BELOW.
         AD(L) = AD(L) + VP*CT/DT
C
         IF (I .LT. NX) THEN
            T = TRANX(L)*XMOB
            AE(L) = T
            AD(L) = AD(L) + T
            AW(L+1) = T
            AD(L+1) = AD(L+1) + T
         END IF
         IF (J .LT. NY) THEN
            T = TRANY(L)*XMOB
            AN(L) = T
            AD(L) = AD(L) + T
            AS(L+NX) = T
            AD(L+NX) = AD(L+NX) + T
         END IF
         IF (K .LT. NZ) THEN
            T = TRANZ(L)*XMOB
            AT(L) = T
            AD(L) = AD(L) + T
            AB(L+NX*NY) = T
            AD(L+NX*NY) = AD(L+NX*NY) + T
         END IF
  200 CONTINUE
  300 CONTINUE
  400 CONTINUE
C
      CALL WELSRC (AD, RHS)
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE SATUPD (ISAT)
C-----------------------------------------------------------------------
C     EXPLICIT SATURATION UPDATE.  ISAT NON ZERO IF ANY CELL LEAVES
C     THE PHYSICAL RANGE BY MORE THAN THE THROW TOLERANCE.
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
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
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
C     --- nativegate: expanded INCLUDE 'GRID.INC' ---
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
C     --- nativegate: end INCLUDE 'GRID.INC' ---
      DATA THROW /0.05D0/
C
      ISAT = 0
      DO 100 L = 1, NCELL
         IF (IACT(L) .EQ. 0) GO TO 100
         DPL = PRES(L) - POLD(L)
         SWAT(L) = SWOLD(L) + 1.0D-5*DPL
         SGAS(L) = SGOLD(L) - 5.0D-6*DPL
         IF (SWAT(L) .LT. -THROW .OR. SWAT(L) .GT. ONE+THROW) ISAT = 1
         IF (SGAS(L) .LT. -THROW .OR. SGAS(L) .GT. ONE+THROW) ISAT = 1
         IF (SWAT(L) .LT. ZERO) SWAT(L) = ZERO
         IF (SGAS(L) .LT. ZERO) SGAS(L) = ZERO
         IF (SWAT(L) + SGAS(L) .GT. ONE) THEN
            S = SWAT(L) + SGAS(L)
            SWAT(L) = SWAT(L)/S
            SGAS(L) = SGAS(L)/S
         END IF
  100 CONTINUE
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION FIPOIL ()
C-----------------------------------------------------------------------
C     OIL IN PLACE, STB.  CALLED BY THE REPORT WRITER.
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
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
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
C     --- nativegate: expanded INCLUDE 'GRID.INC' ---
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
C     --- nativegate: end INCLUDE 'GRID.INC' ---
C
      S = ZERO
      DO 100 L = 1, NCELL
         IF (IACT(L) .EQ. 0) GO TO 100
         CALL PVTSET (PRES(L))
         IF (IERR .NE. 0) GO TO 100
         VP = DXC(L)*DYC(L)*DZC(L)*PORO(L)*NTG(L) / 5.615D0
         SO = ONE - SWAT(L) - SGAS(L)
         IF (SO .LT. ZERO) SO = ZERO
         S  = S + VP*SO/MAX(BO,SMALL)
  100 CONTINUE
      FIPOIL = S
      RETURN
      END